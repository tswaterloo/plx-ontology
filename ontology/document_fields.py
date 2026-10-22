"""Typed, page/span-cited fields of captured business documents.

A captured document (a mail attachment, a SharePoint file) is read from its own pinned
bytes by the deterministic extractor (``integrations.document_understanding``; no model,
no OCR at projection time) and recognised as one *document class* of the profile file
``ontology/schema/document_fields.json``: a supplier confirmation / proforma invoice, a
forwarder quote, a packing slip, ... Each class names its object type and its fields; a
new class is a new profile, never new code (modules are named by
capability, not by transaction kind).

What it decides
---------------
* **Class**: a title cue that starts a line. A ``title_alone`` class's cue fills its whole
  line ("Purchase Order" is a title; "Purchase Order No: 4500" or a table header starting
  "Invoice<tab>" is not) and counts only when no other class's title comes before it, so
  a document of an older class that names a PO or an invoice stays that class. Exactly
  one class must match; none is ``unclassified`` and several are ``ambiguous_class``
  with the gap ``classes_disagree`` citing each (both project nothing). When another
  class has a title line (before or after it), a cue that runs on into prose (two
  lowercase words follow it) is a mention too ("Packing slip must accompany each
  shipment." in a purchase order's terms, "Order confirmation required within 48
  hours." in a note above its title), never a second class, unless it is a prose title:
  its line ends in a number or identifier ("Packing list for order 4500"), or it stands
  above every other class's title line and ends no sentence ("Order confirmation of
  your purchase order" above a "PURCHASE ORDER" line). A prose title beside another
  class's title makes the document ``ambiguous_class``, never one of the two (and a
  mention when that title's class is dismissed by *Direction*, so a PO the own party
  issued is never read as the class its terms name); a cue
  followed by a number or a label ("Order Confirmation No. 77", "Packing list no. 55")
  stays a title. A ``title_alone`` class's title used as the label of its own number
  (its ``number_field``: "Purchase Order: 4500000009") is a numbered title: a title for
  the prose rule, but no class beside another class's title line, wherever that stands
  (a packing list naming "Purchase Order: 4500" ships that PO), and alone a class only
  as the first line of its page with another field of the class stated (else a note,
  delivery note or remittance advice naming the PO would be one). A whole-line title is
  weighed against the document's own numbers: below another class's title whose own
  number the document states (its ``number_field`` read), the title of a class whose
  number it never states is a heading of that document ("PACKING LIST" above the
  packing terms of a purchase order with its "PO Number"), never a second class; with
  both numbers (or neither) stated, or that title first, the document stays
  ``ambiguous_class``.
* **Direction**: a class declaring ``not_issued_by_own_party`` (a customer's purchase
  order) is not the document's class when the own party issued it (status
  ``own_issued`` when no other class remains; a purchase order PLX issued to a supplier
  is no customer's PO): the issuing party on it is an own party (the profile file's
  ``own_parties``) named in an issuer field or, when none is stated, in its letterhead
  block (the unlabelled text of the title's page before the title and before the
  first line or column starting with a whole party-role word, whatever follows it: one
  of the class's recipient words or a label of its text fields but its number, from
  the profile file, so "To: PLX Manufacturing", "Vendor PLX Manufacturing" or "Ship To"
  there heads another party's block, never the issuer's; a labelled value such as
  "Tel: ..." or "Date: ..." is no letterhead but does not end it, and a label with no
  value ends it), and in no recipient field (``own_issued``); or the file's own provenance says so
  (``ISSUED_SIGNALS``): ``issued_category``, the Portal's DocumentCategory confirmed for
  this very version is an issued purchase order, or ``own_mailbox_send``, a mail
  attachment the captured mailbox itself sent. A forwarded or replied copy keeps the
  document's original direction: bytes a captured mailbox
  received from outside the own party (a sender whose domain is no captured mailbox's)
  before it sent them (an outside copy received after the send, a supplier returning
  the own party's PO, decides nothing; with either time unknown it counts) are a
  received document, so that mailbox's own send of the same bytes is no signal, and
  a member of a mail message attached to a send (a message forwarded as an attachment)
  is that inner message's, never the sender's. Never an organisation name elsewhere or
  a number's shape. Without a signal the class stays
  issuer-blind and records the gap ``issuer_unknown``; at read time ``plx.object``
  reports a document whose number an own record the reader may see holds as
  ``issued_by_own_party`` (:func:`own_record_keys`, ``operating/attested_conflicts.py``).
* **Fields**: a label starting a line or a column (after a tab or two spaces), then
  ``:``/``#``, a tab or two spaces, then the value up to the line or column end, or up
  to another of the class's labels with its ``:``/``#`` after a space, which then starts a
  read of its own ("AO LOT#: AO-1 BULK LOT#: BC-2" is two reads). A column gap may cut
  a value (OCR keeps a scan's wide interword spaces): a quantity with no unit of its own
  whose next cell is an unlabelled word is ``unit_unknown`` ('Total Qty: 2012  cases'),
  never the field's default unit, and an identifier field (``identifier_kind`` or
  ``refers_to``) whose next cell is an unlabelled number is ``split_at_gap`` ('PO: 4500
  123'), its page reference marked ``split_at_gap`` too (a lead that says so, never a
  silent prefix). An ``unseparated`` quantity field (a printed form's box: 'Total QTY
  (EA) 41', 'REJECTS 0') also reads its label at a cell start followed by whitespace and a
  number, without ``:``/``#``: the number and the rest of its cell (a second number is
  ``several_values``, an unknown word ``unit_unknown``), cut where another such label and
  its number follow ('Total QTY (EA) 41 Samples (EA) 2' is two reads); a label stating its
  unit ('Samples (EA)') reads the number in that unit when words follow ('Samples (EA) 2
  Completed by'), a contrary unit word is ``unit_unknown``. A label followed by a word is
  no read, and a field's table row is read as a table row only. A field read on several
  pages is one value when every stated read agrees; a blank read of it beside a stated
  one (a form that repeats the label unfilled) is no reading, never a conflict. The
  value is parsed by the field's kind: ``money`` (Decimal text with an explicit
  currency: an ISO code or an unambiguous symbol on the amount, else one consistent
  currency declaration in the document; ``per_unit`` fields also read ``/<unit>``),
  ``quantity`` (Decimal text with a unit the one spelling fold ``ontology.units``
  knows, or the profile's shipping units), ``date`` (ISO; a numeric date only when its
  day/month order is forced), ``code`` (exactly one code of the field's vocabulary, its
  synonyms folded into it: 'CFR (CNF)' is CFR; the rest is the place, untrusted text), ``checkbox`` (one printed box, below) or
  ``text`` (untrusted text). Numbers go
  through ``document_understanding.parse_number``: a decimal comma only when
  unambiguous, never a guess.
* **Checkboxes** (kind ``checkbox``): a box mark after the label (``:``/``#`` or
  whitespace between) or before it at a line or column start (``[x] Shipment released``).
  Only a mark of the profile file's ``checkbox_marks.checked`` is a value (``checked``); a
  ``checkbox_marks.unchecked`` mark is the gap ``unchecked`` (a blank box is unknown, never
  "no"), and no mark or any other mark is the gap ``unreadable``: the default, since OCR
  drops and invents box glyphs. A bare letter (a lone ``x``) is OCR noise as often as a
  mark, so it is ``unreadable`` whatever the profile file lists, and a label with a box on
  both sides (``[x]  Shipment released  [ ]``) is ``unreadable`` citing both boxes: a
  reader cannot tell which one is the label's.
* **Keyed fields** (``keyed``: a pattern of the profile's ``keys``, e.g. a component
  code): the labelled line holds entries ``<key> [=:-] <value>`` separated by ``;``; each
  key gets its own value and citation (a per-component damage count). An
  ``unseparated`` keyed field also reads its label followed by keys on its line ('DAMAGES
  P-0101 = 5; P-0102 = 2'), and a label whose line holds no key ('DAMAGES (part code;
  qty; initial)', 'Damages:') heads the lines below it on its page that start with a key
  (blank lines and one digit-free column header passed, one of two or more cells or
  naming a quantity column; any other line, a section heading, ends them): one
  entry per key, its value the first cell or ``;`` part after it (a digit-free cell after
  it, an initial, is no value; a later part with a digit makes it ``several_values``).
  Entries are never added up; one key read with two values is that entry's
  ``conflicting_values`` gap. A box is the field's ``value_missing`` citing the label
  (blank on the paper) only when nothing but its printed parenthetical or a fill line
  follows the label and a known label or section heading (or the page end) ends it;
  any other text there, a note ('see attached') or letters OCR made of handwriting, is
  box content no key was read from: ``label_seen_unread`` (``no_key``), never blank. A
  line of letters and marks with no word of three letters and no digit (handwriting OCR
  garbled) does not end the box, so a key below it is read; when keys are read, such
  text and the label line's own unkeyed text are the field's ``key_missing``, so the
  entries never look complete. On a **layout page** (below) whose label line's next cell
  is the next section (a class label), the box is the label's column, cut there only at
  a cell boundary: a cell written across that column leaves the box unread, never cut
  into a shorter key; any other next cell is the box's own handwriting.
* **Tables**: one scanner reads every table, a class's line items and its table cells
  alike. Cells split by a tab, two spaces or Markdown pipes (OCR keeps a scan's column
  gaps: tesseract preserves interword spaces); a header line of two or more cells heads
  one table of one kind, so no cell is read twice: a **line-items** header when every cell
  is the label of a distinct column of the profile's ``line_items``, else a **cell**
  header when it names a column of a field's ``cell`` after its first cell. A following
  line with as many cells, one not blank, is a row cited cell by cell; any other line ends
  the table (a blank line, a total; one of two or more cells is the gap ``unaligned``: its
  cells are never fitted to columns by count), except a row label's row below, a row
  naming a row label as one option of a ``/`` list and a blank Markdown row of a cell
  table. A pipe with a space on both sides inside a gap-split cell is a printed column
  rule ('Start | Finish', OCR of a ruled header) and splits it; a pipe in a word ('|.')
  does not. A **layout page** is an OCR page whose pinned text ends in
  ``integrations.document_understanding.LAYOUT_MARK``: text the extractor rebuilt from
  tesseract word boxes (``ocr_layout_text``: each word at its column). Only there a cell
  table also keeps a row whose cells each lie under exactly one header cell or past the
  header's right edge; OCR text pinned without the mark (plain psm-3 text, where a
  dropped cell shifts the row left) keeps the count rule.
* **Line items** (a profile's ``line_items``): each row of a line-items table is one
  item, every cell typed by its column's kind and cited by its own span (a bare quantity
  takes the ``unit_from`` cell as its unit, a ``per_unit_of`` price without its own unit
  is per that row's quantity unit; the value names the cell it borrowed from). A blank or
  unreadable cell is that cell's gap (a PDF text layer that puts each cell on its own line
  yields no items, never guesses). A line-items row is checked by its cell count only.
* **Table cells** (a field's ``cell``: ``rows`` labels x ``columns`` headers): a form that
  states a quantity per row of a table (a shop-floor sheet's Filling and Total rows under
  QTY (EA)) is read by the same scanner. Below a cell header naming one of the field's
  columns, a row whose first cell is one of its row labels gives the cell in that column,
  typed by the field's kind and cited by the cell's own span; it is one more read of the
  field, beside its labelled reads (two Filling rows with two quantities are
  ``conflicting_values``, never a sum). Such a row with another number of cells than its
  header is the gap ``unaligned`` citing the row and keeps the table, except on a layout
  page, where the one row cell after the label lying under the column's header cell alone
  (by x-overlap, no other cell overlapping that column) is the read; a handwritten cell
  OCR dropped elsewhere in the row no longer hides it (decision
  ``docs/decisions/2026-10-18-ao-ocr-layout-columns.md``). A row whose first cell lists the row label as one option of several
  ('Filling/Setup/Tear down', or ruled 'Filling | Setup | Tear down' with a speck
  before an option; the task chosen by an underline the text does not hold) is
  the gap ``unreadable`` citing the row, never that task's value. A field no cell
  addresses is never read from a table, and a row still unreadable (a single-spaced OCR
  row) leaves its row label seen (``label_seen_unread``).
* **Labels**: one label-to-field map per class (each label and row label, folded) serves
  the labelled reads, the table rows and the seen labels.
* **Gaps**: an unstated, unreadable or ambiguous field is a gap with a reason and the
  citations it saw, never a value (a blank paper field is ``value_missing``: unknown,
  never zero; a number followed by ``?`` is ``unreadable``): ``not_stated`` (the
  document never names the field: none of its labels or row labels starts a cell),
  ``label_seen_unread`` (it names the field there, but no value was read: a reader
  limit, never the document's silence; it cites up to ``MAX_SEEN_CITATIONS`` of those
  labels and names why in ``unread``, one of ``UNREAD_REASONS``: ``no_key`` a keyed
  box with text but no key, ``handwritten_unkeyed`` such a box on a layout page at
  least ``HANDWRITTEN_SHARE`` of whose text OCR read in words below
  ``integrations.document_understanding.HANDWRITING_CONFIDENCE`` (handwritten part codes
  OCR cannot key: a typed reader limit,
  also citing the box text; never a key mapped out of it; decision
  ``docs/decisions/2026-10-19-ao-handwritten-reader-limit.md``), ``single_spaced`` a value one space after the label,
  ``value_unplaced`` a plain label, ``row_unaligned`` a row label under a header naming
  the field, ``header_unseen`` a row label with no such header above; a label seen only
  as one option of a ``/`` list is ``unreadable`` instead: the mark that picks the option
  is on the paper, not in the text),
  ``value_missing``, ``unchecked``, ``unreadable`` (also a table cell with an unclear
  digit, ``0.0?``; a text field's prose may hold "carton 3? damaged"),
  ``decimal_separator_ambiguous``, ``number_unreadable``, ``currency_unknown``,
  ``currency_conflict``, ``unit_unknown``, ``split_at_gap``, ``date_ambiguous``,
  ``several_values`` (a quantity field with a ``default_unit`` stated in several distinct
  units at once, '1509EA; 378CS', is not: it reads in its own unit and keeps the others
  as ``alternates``, each cited; reads conflict only within one unit, and an
  alternate unit read with two values is listed in ``alternate_conflicts``),
  ``conflicting_values`` (one label read twice with different values), ``too_long``
  (also a table over ``MAX_LINE_ITEMS`` rows), ``unaligned`` (a table row, of the line
  items or of a field's table cell).
* **Pages not read**: of a PDF over the extractor's page bound only pages
  1-``MAX_PAGES`` are read; the result names the rest (``skipped_by_cap``: ``first_page``,
  ``last_page``, ``cap`` ``pages``, ``limit``) and carries the gap ``pages`` /
  ``skipped_by_cap``, and a field no read page states is ``not_stated_in_read_pages``
  (it may be stated on a page not read), never ``not_stated``.
* **Currency conflicts**: an amount whose own currency disagrees with another currency
  the amount or the document states (``USD`` on the amount, ``Currency: CAD`` declared;
  one field read as USD and as CAD) is the gap ``currency_conflict`` listing every
  ``readings`` entry (value, currency, ``inline`` or ``declared``): never a value, never
  coerced to one currency, converted or summed. ``plx.object`` reports it as a typed
  conflict (``operating/attested_conflicts.py``).
* **Reader**: every field read carries ``read_by``: ``parser`` (the deterministic parser
  over a text layer) or ``ocr`` (the same parser over OCR text: of a scan's capture, or a
  page the extractor OCR-read), per value, carried into ``plx.object`` values. An OCR
  number is never a verified value (a person confirms it).
* **Citations**: every value and gap cites the bytes it was read from: ``l1_handle``
  (the byte-pinned capture handle), ``source_sha256``, ``page`` (1-based, a PDF) and
  ``start``/``end`` offsets into that page's extracted text (the whole text when
  unpaged), plus the literal ``text``. A scanned PDF (no text layer) is read from the
  capture's own OCR text (``integrations.captured_text``, ``extracted_ocr``; a SharePoint
  file and a mail attachment alike):
  its citations also name ``text_sha256`` (the pinned OCR text) with ``text_source``
  ``ocr``. :func:`resolve_citation` re-reads the bytes (and that text), checks their
  SHA-256 and returns the cited literal, so a citation is checkable against the
  original, not against a stored copy.
* **References**: identifiers the one identifier grammar finds in the text
  (``ontology.mentions.extract``), cited the same way. A field declaring ``refers_to``
  (an identifier kind: a supplier confirmation's ``po_reference`` names a Petra PO) marks
  the grammar's reading of that kind inside its value's span with the field, else adds
  its value as the grammar reads it after one of the field's labels (never a value the
  grammar refuses: 'PO: 12' is too short), cited by the field's span;
  the reference links the record holding the identifier (a lead mention), and the field
  stays text: never an identifier of the document itself. A field declaring
  ``refers_to_property`` (a certificate's supplier ``lot`` names
  ``ReceiptLine.supplier_lot_number``) names its value as that one property holds it: its
  value as the kind's normaliser reads it (the label makes it one), resolved only on index
  rows of that kind, type and property, never through the kind's alias kinds (a supplier
  lot is never a PLX lab id or batch code); every other reading inside its span is dropped
  (an RP-shaped supplier lot is no receipt lab id).

Every result is a machine-read claim at tier ``lead`` with ``external_assertion``
true (the author of a captured file is not known): never verified, never a
confirmation of a payment, receipt or quantity (``ontology.mentions.EXTERNAL_UNCONFIRMABLE``).
Text inside a document is data: an instruction in it is at most an untrusted ``text``
field value; it never selects a class, a field or an action.

Projection
----------
:func:`captured` turns the captured attachments / drive items of one capture source into
records of relation ``documentFields`` (one per classified document version, keyed by
the source record and the class, cited by the byte-pinned handle, same audience and
validity interval as the source version; its ``evidence`` states the handle and the
byte / OCR text digests once, each citation only page, span and literal:
:func:`full_citation`); each member of a captured zip/.msg/.eml is a document of its own,
keyed by the source record and the member path and cited by the member
(``integrations.captured_text.member_handle``: the member's SHA-256 inside the
container's pinned bytes); ``ontology/project.py`` serves that relation
from ``MailCaptures`` and ``SharePointCaptures`` and the schema binds it per class
(``where profile = <class>``). :func:`store_document_links` (a ``build_store``
``post_project`` step) writes each reference as ``lead`` links of the class type's
declared ``mention`` link types (``SupplierConfirmation.confirms`` ...) into the same
generation, through the generation's identifier index (``mentions``' candidate lookup).

Comparison
----------
A class may declare what it ``attests`` about a record type: field / property pairs
reached over declared link paths, an event, or a keyed field matched per key through a
key path (:func:`comparisons`). ``plx.object`` compares them at read time over the
values that reader may see and reports each disagreement as a typed conflict, never a
pick; a blank paper field facing a stated value is a conflict too (unknown, not equal).
Money compares only in one stated currency and on one declared tax basis: a money field
may declare the ``tax_basis`` its labels state (``ontology/units.py`` ``TAX_BASES``;
omitted is ``unknown``), the record side's is its schema source's; any other pair is a
``currency_*`` / ``tax_basis_*`` gap, never a conflict and never a match. A value the
document states that the record holds empty is a ``missing_in_record`` lead; a rule may
name the owner-system proposal type (``propose``) it may be proposed through
(``operating/attested_conflicts.py``). Every key of the profile file is known: a misspelt
one is refused when the file loads.

:func:`capture_citation_reader` re-reads a byte-pinned capture citation of a projected
document as the harness's evidence (``evals``): the reader's item gate, the pinned
bytes (and OCR text) re-extracted, never the projection store. The capability guards
read a cited SharePoint file or mail attachment the same way (:func:`pinned_document_row`
over the bytes the case host's capture gate serves, ``operating/capability_guards.py``).

Stdlib only (the PDF reader is the extractor's optional ``pypdf``).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
import functools
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Callable, Container, Iterable, Iterator, Mapping, NamedTuple

from integrations import document_understanding as du
from integrations.captured_text import (OCR_UNAVAILABLE, TEXT_OCR, TEXT_READ, CapturedText,
                                        Member, NotRemembered, ReadingMemo,
                                        byte_pinned, captured_text, container_members,
                                        member_handle, ocr_digest, ocr_failure_cause,
                                        read_blob, read_text, text_status_kind)
from ontology import identifiers, mentions
from ontology.store_ddl import MARKINGS
from ontology.units import (TAX_BASES, UOM_CANONICAL, currency_reading,
                            currency_token_pattern, unit_token)

FORMAT = "plx-document-fields/v1"
PROFILES_PATH = Path(__file__).resolve().parent / "schema" / "document_fields.json"
PROFILES_FORMAT = "plx-document-field-profiles/v1"
RELATION = "documentFields"
TIER = "lead"
READ_BY = ("parser", "ocr")   # who read a value: the deterministic parser, over OCR text
KINDS = ("money", "quantity", "date", "code", "text", "checkbox")
CHECKED = "checked"   # the one value a checkbox field states
MAX_TEXT_VALUE = 200


@functools.lru_cache(maxsize=1)
def profiles() -> dict[str, Any]:
    """The profile file, checked: unique class ids, known kinds, one field per label."""
    data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    if data.get("format") != PROFILES_FORMAT:
        raise ValueError("document_field_profiles_format")
    marks = data["checkbox_marks"]
    if not marks["checked"] or set(map(str.casefold, marks["checked"])) & \
            set(map(str.casefold, marks["unchecked"])):
        raise ValueError("document_field_checkbox_marks_invalid")
    for name, synonyms in (data.get("code_synonyms") or {}).items():
        words = set(data["vocabularies"].get(name, ()))
        if not words or any(w not in words or c not in words or c in synonyms
                            for w, c in synonyms.items()):
            raise ValueError(f"document_field_code_synonyms_invalid:{name}")
    stops = data.get("stop_labels")
    if not isinstance(stops, dict) or set(stops) != {"description", "labels"} or \
            not stops["labels"] or not all(isinstance(w, str) and w.strip()
                                          for w in stops["labels"]):
        raise ValueError("document_field_stop_labels_invalid")
    seen: set[str] = set()
    for profile in data["profiles"]:
        if profile["id"] in seen:
            raise ValueError(f"document_field_profile_duplicate:{profile['id']}")
        seen.add(profile["id"])
        _known_keys(profile, _PROFILE_KEYS, f"profile:{profile['id']}")
        if not isinstance(profile.get("title_alone", False), bool):
            raise ValueError(f"document_field_title_alone_invalid:{profile['id']}")
        labels: set[str] = set()
        for spec in profile["fields"]:
            _known_keys(spec, _FIELD_KEYS, f"field:{spec.get('name')}")
            if spec["kind"] not in KINDS:
                raise ValueError(f"document_field_kind_unknown:{spec['kind']}")
            if spec["kind"] == "code" and spec.get("vocabulary") not in data["vocabularies"]:
                raise ValueError(f"document_field_vocabulary_unknown:{spec['name']}")
            if spec["kind"] == "checkbox" and set(spec) - {"name", "kind", "labels",
                                                           "description"}:
                raise ValueError(f"document_field_checkbox_invalid:{spec['name']}")
            if "keyed" in spec and (spec["kind"] != "quantity"
                                    or spec["keyed"] not in data.get("keys", {})):
                raise ValueError(f"document_field_key_unknown:{spec['name']}")
            if "unseparated" in spec and (spec["unseparated"] is not True
                                          or spec["kind"] != "quantity"):
                raise ValueError(f"document_field_unseparated_invalid:{spec['name']}")
            if "tax_basis" in spec and (spec["kind"] != "money"
                                        or spec["tax_basis"] not in TAX_BASES):
                raise ValueError(f"document_field_tax_basis_unknown:{spec['name']}")
            if "marking" in spec and spec["marking"] not in MARKINGS:
                raise ValueError(f"document_field_marking_unknown:{spec['name']}")
            if "refers_to" in spec and (spec["kind"] != "text" or "identifier_kind" in spec
                                        or spec["refers_to"] not in identifiers.kinds()):
                raise ValueError(f"document_field_refers_to_invalid:{spec['name']}")
            if "refers_to_property" in spec:
                target = spec["refers_to_property"]
                if spec["kind"] != "text" or {"identifier_kind", "refers_to"} & set(spec) or \
                        not isinstance(target, dict) or set(target) != {
                            "object_type", "property", "identifier_kind"} or \
                        target["identifier_kind"] not in identifiers.kinds():
                    raise ValueError(f"document_field_refers_to_property_invalid:{spec['name']}")
            for label in spec["labels"]:
                if label.casefold() in labels:
                    raise ValueError(f"document_field_label_duplicate:{label}")
                labels.add(label.casefold())
        _check_cells(profile)
        names = {spec["name"]: spec for spec in profile["fields"]}
        if "number_field" in profile and (names.get(profile["number_field"]) or {}).get(
                "kind") != "text":
            raise ValueError(f"document_field_number_field_invalid:{profile['id']}")
        if "not_issued_by_own_party" in profile:
            _check_issuer(profile, names, data)
        if "attests" in profile:
            _known_keys(profile["attests"], {"link_type", "compare"}, "attests")
        for rule in (profile.get("attests") or {}).get("compare", ()):
            _known_keys(rule, _RULE_KEYS, f"compare:{rule.get('field')}")
            spec = names.get(rule["field"])
            if spec is None or ("key_path" in rule) != ("keyed" in spec) or \
                    ("event" in rule) == ("property" in rule) or ("propose" in rule and (
                        "keyed" in spec or not isinstance(rule["propose"], str))):
                raise ValueError(f"document_field_compare_invalid:{rule['field']}")
        if "line_items" in profile:
            _check_line_items(profile["line_items"], profile["id"], data["vocabularies"])
    return data


_PROFILE_KEYS = {"id", "object_type", "noun", "description", "titles", "title_alone",
                 "number_field", "not_issued_by_own_party", "fields", "attests",
                 "line_items"}
# identifier_kind, quantity_role, count, description, property, marking (and a profile's
# noun): how the schema loader names, types and marks the field's property
# (ontology/schema.py derive_document_types); the reader ignores them.
_FIELD_KEYS = {"name", "kind", "labels", "vocabulary", "keyed", "default_unit", "per_unit",
               "tax_basis", "identifier_kind", "quantity_role", "count", "description",
               "property", "marking", "refers_to", "refers_to_property", "cell",
               "unseparated"}
_RULE_KEYS = {"field", "property", "event", "path", "key_path", "key_property", "propose"}
_COLUMN_KEYS = {"name", "kind", "labels", "vocabulary", "unit_from", "per_unit_of"}
_COLUMN_KINDS = ("text", "quantity", "money", "date", "code")


def _known_keys(entry: Mapping[str, Any], allowed: set[str], where: str) -> None:
    """A profile entry names only keys the reader knows: a misspelt key is refused, never
    silently ignored."""
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ValueError(f"document_field_key_unknown:{where}:{unknown[0]}")


def _check_cells(profile: Mapping[str, Any]) -> None:
    """A field's ``cell`` (a table cell by row label x column header) names row labels
    and column headers; it is a plain field (not keyed, not a checkbox), and its row labels
    have one owner (:func:`_owners`)."""
    for spec in profile["fields"]:
        if "cell" not in spec:
            continue
        cell = spec["cell"]
        _known_keys(cell, {"rows", "columns"}, f"cell:{spec['name']}")
        words = [*cell.get("rows", ()), *cell.get("columns", ())]
        if spec["kind"] == "checkbox" or "keyed" in spec or not cell.get("rows") or \
                not cell.get("columns") or \
                not all(isinstance(w, str) and _label_key(w) for w in words):
            raise ValueError(f"document_field_cell_invalid:{spec['name']}")
    _owners(profile)


def _owners(profile: Mapping[str, Any]) -> dict[str, str]:
    """The one label-to-field map of a class: each label and table row label of its fields
    (``cell.rows``), folded by :func:`_label_key`, to the field it names. A label folding
    into another field's label is refused, and so is a row label that is another field's
    label or row label (its read and its seen label have one owner)."""
    owner: dict[str, str] = {}
    for spec in profile["fields"]:
        for label in spec["labels"]:
            if owner.setdefault(_label_key(label), spec["name"]) != spec["name"]:
                raise ValueError(f"document_field_label_duplicate:{label}")
    for spec in profile["fields"]:
        for row in (spec.get("cell") or {}).get("rows", ()):
            if owner.setdefault(_label_key(row), spec["name"]) != spec["name"]:
                raise ValueError(f"document_field_cell_invalid:{spec['name']}")
    return owner


def _profile(profile_id: str) -> Mapping[str, Any]:
    return next(p for p in profiles()["profiles"] if p["id"] == profile_id)


@functools.lru_cache(maxsize=16)
def _owner(profile_id: str) -> dict[str, str]:
    """:func:`_owners` of a class of the profile file."""
    return _owners(_profile(profile_id))


def _check_issuer(profile: Mapping[str, Any], names: Mapping[str, Any],
                  data: Mapping[str, Any]) -> None:
    """``not_issued_by_own_party``: issuer and recipient fields are text fields of the
    profile, and the file names the own parties to look for."""
    rule = profile["not_issued_by_own_party"]
    _known_keys(rule, {"issuer_fields", "recipient_fields", "recipient_lines", "own_record"},
                f"issuer:{profile['id']}")
    own = rule.get("own_record") or {}
    _known_keys(own, {"field", "identifier_kind"}, f"own_record:{profile['id']}")
    fields = [*rule.get("issuer_fields", ()), *rule.get("recipient_fields", ()),
              *([own.get("field")] if own else [])]
    if not rule.get("issuer_fields") or not (data.get("own_parties") or {}).get("names") or \
            any((names.get(name) or {}).get("kind") != "text" for name in fields) or \
            (own and own.get("identifier_kind") not in identifiers.kinds()) or \
            not all(isinstance(w, str) and w.strip() for w in rule.get("recipient_lines", ())):
        raise ValueError(f"document_field_issuer_invalid:{profile['id']}")


def _check_line_items(spec: Mapping[str, Any], profile_id: str,
                      vocabularies: Mapping[str, Any]) -> None:
    """A line-item table: two or more typed columns with distinct names and header
    labels (a table of text columns too: a test result is 'Conforms' as often as a
    number); a quantity's ``unit_from`` names a text column, a money column's
    ``per_unit_of`` a quantity column."""
    _known_keys(spec, {"columns"}, f"line_items:{profile_id}")
    columns = {column["name"]: column for column in spec["columns"]}
    labels = [label.casefold() for column in spec["columns"] for label in column["labels"]]
    if len(columns) != len(spec["columns"]) or len(set(labels)) != len(labels) or \
            len(columns) < 2:
        raise ValueError(f"document_field_line_items_invalid:{profile_id}")
    for column in spec["columns"]:
        _known_keys(column, _COLUMN_KEYS, f"column:{column.get('name')}")
        source = columns.get(column.get("unit_from", column.get("per_unit_of")))
        if column["kind"] not in _COLUMN_KINDS or \
                (column["kind"] == "code" and column.get("vocabulary") not in vocabularies) or \
                ("unit_from" in column and (column["kind"] != "quantity" or source is None
                                            or source["kind"] != "text")) or \
                ("per_unit_of" in column and (column["kind"] != "money" or source is None
                                              or source["kind"] != "quantity")):
            raise ValueError(f"document_field_line_items_invalid:{column['name']}")


def extractor_version() -> str:
    """Extractor id plus the digest of the profile file it ran with."""
    return (f"{FORMAT}+{du.EXTRACTOR_VERSION}+profiles@"
            f"{sha256(PROFILES_PATH.read_bytes()).hexdigest()[:16]}")


def _alternation(words: Iterable[str]) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"[ \t]+")
                    for w in sorted(set(words), key=lambda w: (-len(w), w)))


@functools.lru_cache(maxsize=16)
def _label_pattern(profile_id: str) -> tuple[re.Pattern[str], dict[str, str]]:
    """The labelled-value pattern of a class's labels but its checkboxes', and the one
    label-to-field map (:func:`_owner`) its matched labels are looked up in."""
    labels = [label for spec in _profile(profile_id)["fields"] if spec["kind"] != "checkbox"
              for label in spec["labels"]]
    return _labelled(labels, stops=profiles()["stop_labels"]["labels"]), _owner(profile_id)


@functools.lru_cache(maxsize=16)
def _declared_labels(profile_id: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Whether a text is declared labels alone, each with its ':' or '#' (a value that is
    itself a label: no value), and whether a line is a row of two or more of them (a
    header row: no value beside any). Declared: the class's field labels, its checkbox
    labels and the profile file's ``stop_labels``; never a word a document happens to
    end in ':'."""
    words = _alternation([*(label for spec in _profile(profile_id)["fields"]
                            for label in spec["labels"]),
                          *profiles()["stop_labels"]["labels"]])
    one = r"(?:" + words + r")\.?[ \t]*(?:#[ \t]*:|[:#])[ \t]*"
    return (re.compile(r"[ \t]*(?:" + one + r")+", re.IGNORECASE),
            re.compile(r"[ \t]*(?:" + one + r"){2,}", re.IGNORECASE))


# A box as OCR writes it: bracketed, one glyph or a lone x. Which marks read as checked or
# unchecked is the profile file's ``checkbox_marks``; any other is unreadable.
_BOX = r"\[[^\]\n]{0,3}\]|\([^)\n]{0,3}\)|[^\w\s]|x"


@functools.lru_cache(maxsize=16)
def _checkbox_pattern(profile_id: str) -> re.Pattern[str] | None:
    """The labels of a class's checkbox fields with their box: before the label at a line
    or column start (group ``before``; ``after``: a second box after that label), else
    after it (group ``value``, up to the line or column end, the separator optional). A
    label of the class's other fields that starts with a checkbox label ("Approved by:"
    beside an "Approved" box) is that field's, never the box's."""
    profile = next(p for p in profiles()["profiles"] if p["id"] == profile_id)
    labels = [label for spec in profile["fields"] if spec["kind"] == "checkbox"
              for label in spec["labels"]]
    if not labels:
        return None
    words = _alternation(labels)
    longer = [label for spec in profile["fields"] if spec["kind"] != "checkbox"
              for label in spec["labels"] if any(
                  label.casefold().startswith(box.casefold() + " ") for box in labels)]
    other = (r"(?!(?:" + _alternation(longer) + r")\.?[ \t]*(?:#[ \t]*:|[:#]))"
             if longer else "")
    return re.compile(
        r"(?:^|(?<=\t)|(?<=  ))[ \t]*(?:(?P<before>" + _BOX + r")[ \t]*(?P<blabel>" + words +
        r")\b\.?(?:[ \t]*[:#]?[ \t]*(?P<after>(?![:#])(?:" + _BOX + r")))?[ \t]*(?=\t|  |$)"
        r"|" + other + r"(?P<label>" + words + r")\b\.?(?:[ \t]*[:#][ \t]*|[ \t]+)?"
        r"(?P<value>.*?)[ \t]*(?=\t|  |$))", re.IGNORECASE)


def _labelled(labels: Iterable[str], *, loose: bool = False,
              stops: Iterable[str] = ()) -> re.Pattern[str]:
    # A label starts a line or a column and is followed by ':' or '#' (``loose``: or
    # by whitespace, "All prices in USD"); its value runs to the line or column end or
    # (not ``loose``) to another declared label and its ':' or '#' after a space: one of
    # ``labels`` (:func:`_labelled_values` reads that one from there) or of ``stops``
    # (a label no field is read from: 'Phone:', 'Attn:'). An undeclared word ending in
    # ':' stays in the value. A bare label (a table header) is no read.
    labels = list(labels)
    words = _alternation(labels)
    strict = r"[ \t]*(?:#[ \t]*:|[:#])"
    separator = strict + r"[ \t]*" + (r"|[ \t]+" if loose else "")
    following = "" if loose else \
        r"|(?<=[ \t])(?:" + _alternation([*labels, *stops]) + r")\.?" + strict
    return re.compile(
        r"(?:^|(?<=\t)|(?<=  ))[ \t]*(?P<label>" + words + r")\.?"
        r"(?:" + separator + r")(?P<value>.*?)[ \t]*(?=" + du.COLUMN_GAP + "|$" + following +
        r")",
        re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _title_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    # A ``title_alone`` cue fills its whole line: "Purchase Order No: 4500", a header
    # cell "Purchase Order<tab>..." or "Purchase order acknowledgement" is no title.
    return tuple((p["id"], re.compile(r"^[ \t]*(?:" + _alternation(p["titles"]) + r")" +
                                      (r"[ \t]*$" if p.get("title_alone") else r"\b"),
                                      re.IGNORECASE | re.MULTILINE))
                 for p in profiles()["profiles"])


@functools.lru_cache(maxsize=1)
def _numbered_title_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    # A ``title_alone`` class's title as the label of its own number, a value with a
    # digit after it: "Purchase Order: 4500000009", "Purchase Order #4500".
    out = []
    for p in profiles()["profiles"]:
        number = next((f for f in p["fields"] if f["name"] == p.get("number_field")), None)
        titles = {t.casefold() for t in p["titles"]}
        words = [label for label in (number or {}).get("labels", ())
                 if label.casefold() in titles]
        if p.get("title_alone") and words:
            out.append((p["id"], re.compile(
                r"^[ \t]*(?:" + _alternation(words) + r")(?=\.?[ \t]*(?:#[ \t]*:|[:#])"
                r"[^\n]*\d)", re.IGNORECASE | re.MULTILINE)))
    return tuple(out)


def _stated(profile: Mapping[str, Any], pages: list[tuple[int | None, str]]) -> set[str]:
    """The class's fields the document states a labelled value for (their names)."""
    pattern, owner = _label_pattern(profile["id"])
    return {owner[label] for label, value, _, _ in
            _labelled_values(pattern, pages, lambda *_: None) if value.strip()}


@functools.lru_cache(maxsize=16)
def _party_line_pattern(profile_id: str) -> re.Pattern[str] | None:
    """A line or column starting with a whole party-role word of the class, whatever
    follows it (a colon, a tab, a space, a name): one of its ``recipient_lines`` ("To",
    "Attn") or a label of its text fields but its number ("Vendor", "Ship To",
    "Supplier"). Another party's block starts there; a word running on with a hyphen
    ("To-Go Foods") is none."""
    profile = next(p for p in profiles()["profiles"] if p["id"] == profile_id)
    words = [*profile["not_issued_by_own_party"].get("recipient_lines", ()),
             *(label for spec in profile["fields"] if spec["kind"] == "text"
               and spec["name"] != profile.get("number_field") for label in spec["labels"])]
    return re.compile(r"(?:^|(?<=\t)|(?<=  ))[ \t]*(?:" + _alternation(words) + r")(?![\w-])",
                      re.IGNORECASE | re.MULTILINE) if words else None


@functools.lru_cache(maxsize=1)
def _own_party_pattern() -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + _alternation(profiles()["own_parties"]["names"]) +
                      r")(?!\w)", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _declaration_pattern() -> re.Pattern[str]:
    return _labelled(profiles()["currencies"]["declaration_labels"], loose=True)


# --------------------------------------------------------------------------- values

_MONEY = re.compile(
    r"^(?P<approx>~|approx\.?[ \t]*|ca\.[ \t]*)?(?P<pre>" + currency_token_pattern() +
    r")?[ \t]*(?P<num>" + du.NUMBER_TOKEN.pattern + r")[ \t]*(?P<post>" +
    currency_token_pattern() + r")?"
    r"(?:[ \t]*(?:/|per[ \t]+)[ \t]*(?P<per>[A-Za-z][A-Za-z0-9.]*))?[ \t]*(?P<rest>.*)$")
_QUANTITY = re.compile(r"^(?P<approx>~)?[ \t]*(?P<num>" + du.NUMBER_TOKEN.pattern +
                       r")[ \t]*(?P<unit>[A-Za-z][A-Za-z.]*)?[ \t]*(?P<rest>.*)$")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul",
                                       "aug", "sep", "oct", "nov", "dec"), 1)}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")
# A digit the writer left unclear ("150?", "0.0?"): unreadable, never a value.
_UNCLEAR = re.compile(r"\d\?")
_NAMED_DATE = re.compile(r"^(?:(\d{1,2})[ \t-]+([A-Za-z]{3,9})\.?,?[ \t-]+(\d{4})|"
                         r"([A-Za-z]{3,9})\.?[ \t]+(\d{1,2}),?[ \t]+(\d{4}))$")


def _unit(word: str | None, spec: Mapping[str, Any]) -> str | None:
    """The folded unit of a written unit word, or of the field's default; None if unknown."""
    if word is None:
        return spec.get("default_unit")
    word = word.rstrip(".").lower()
    folded = unit_token(word)
    return folded if folded in UOM_CANONICAL or word in profiles()["units"] else None


def _currency(pre: str | None, post: str | None, declared: tuple[str, ...]
              ) -> tuple[str | None, str | None, str, list[tuple[str, str]]]:
    """(currency, problem, basis, stated) for an amount: ``inline`` (a code or an
    unambiguous symbol on the amount, agreeing with every currency the document declares)
    or ``declared`` (the document's one currency declaration, consistent with an ambiguous
    symbol). ``stated``: the (code, basis) pairs a ``currency_conflict`` is between. Each
    token is read from the one table in ``ontology/units.py`` (``currency_reading``)."""
    found = []
    ambiguous: frozenset[str] | None = None
    for token in (pre, post):
        if token is None:
            continue
        code, candidates = currency_reading(token)
        if code is not None:
            found.append(code)
        elif candidates:
            ambiguous = candidates
        else:
            return None, "currency_unknown", "none", []
    stated = [(code, "inline") for code in dict.fromkeys(found)] + \
        [(code, "declared") for code in declared]
    if found:
        if len(set(found)) > 1 or set(declared) - {found[0]} or (
                ambiguous is not None and found[0] not in ambiguous):
            return None, "currency_conflict", "none", stated
        return found[0], None, "inline", []
    if len(declared) == 1 and (ambiguous is None or declared[0] in ambiguous):
        return declared[0], None, "declared", []
    if len(declared) > 1:
        return None, "currency_conflict", "none", stated
    return None, "currency_unknown", "none", []


def _date(raw: str) -> tuple[str | None, str | None]:
    raw = raw.strip()
    try:
        if (m := _ISO_DATE.match(raw)):
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat(), None
        if (m := _NUMERIC_DATE.match(raw)):
            a, b, year = int(m[1]), int(m[2]), int(m[3])
            if a > 12 >= b:
                return date(year, b, a).isoformat(), None
            if b > 12 >= a:
                return date(year, a, b).isoformat(), None
            return (None, "date_ambiguous") if a != b else (date(year, a, b).isoformat(), None)
        if (m := _NAMED_DATE.match(raw)):
            day, month, year = (m[1], m[2], m[3]) if m[1] else (m[5], m[4], m[6])
            number = _MONTHS.get(month[:3].lower())
            if number is not None and (len(month) == 3 or month.lower().startswith(
                    ("january", "february", "march", "april", "may", "june", "july",
                     "august", "september", "october", "november", "december")[number - 1])):
                return date(int(year), number, int(day)).isoformat(), None
    except ValueError:
        return None, "unreadable"
    return None, "unreadable"


def code_fold(word: str) -> str:
    """A vocabulary word as its code: upper-cased, a synonym folded into the code it
    names (the profile file's ``code_synonyms``: 'CNF' is 'CFR')."""
    word = word.upper()
    for synonyms in (profiles().get("code_synonyms") or {}).values():
        word = synonyms.get(word, word)
    return word


def _parse(spec: Mapping[str, Any], raw: str, convention: str | None,
           declared: tuple[str, ...], *, cell: bool = False, past: str = ""
           ) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    """(typed value, None, []) or (None, gap reason, readings) for one labelled value
    (``cell``: a table cell; ``past``: the unlabelled cell after it, :func:`_labelled_values`);
    ``readings`` only for a ``currency_conflict`` (module docstring)."""
    kind = spec["kind"]
    if not raw and kind != "checkbox":   # no box read is unreadable, never blank
        return None, "value_missing", []
    if kind == "text":
        if cell and _UNCLEAR.search(raw):   # a cell's unclear digit; prose may hold "3?"
            return None, "unreadable", []
        if past[:1].isdigit() and ("identifier_kind" in spec or "refers_to" in spec):
            return None, SPLIT_AT_GAP, []   # 'PO: 4500  123': one number or two
        return ({"value": raw}, None, []) if len(raw) <= MAX_TEXT_VALUE else \
            (None, "too_long", [])
    if kind == "date":
        value, problem = _date(raw)
        return (None, problem, []) if value is None else ({"value": value}, None, [])
    if kind == "code":
        vocabulary = profiles()["vocabularies"][spec["vocabulary"]]
        words = re.compile(r"\b(" + "|".join(vocabulary) + r")\b", re.IGNORECASE)
        codes = {code_fold(m.group(1)) for m in words.finditer(raw)}
        if len(codes) != 1:
            return None, ("several_values" if codes else "unreadable"), []
        # Every word of the one code goes, a synonym in brackets too: 'CFR (CNF) Shanghai'.
        place = re.sub(r"\(\s*\)", "", words.sub("", raw)).strip(" \t,.;:-")
        return {"value": codes.pop(), **({"place": place} if place else {})}, None, []
    if kind == "checkbox":
        mark = raw.strip().casefold()
        marks = profiles()["checkbox_marks"]
        if mark in map(str.casefold, marks["checked"]) and not mark.isalnum():
            return {"value": CHECKED}, None, []
        return None, ("unchecked" if mark and mark in map(str.casefold, marks["unchecked"])
                      else "unreadable"), []
    if kind == "quantity" and (stated := _in_units(spec, raw, convention)) is not None:
        return stated, None, []
    m = (_MONEY if kind == "money" else _QUANTITY).match(raw)
    if m is None or m["rest"].startswith("?"):   # "150?": a digit the writer left unclear
        return None, "unreadable", []
    if du.NUMBER_TOKEN.search(m["rest"]):
        return None, "several_values", []
    number, problem = du.parse_number(m["num"], convention)
    if number is None:
        return None, problem, []
    out: dict[str, Any] = {"value": _plain(number)}
    if m["approx"]:
        out["approximate"] = True
    if m["rest"]:
        out["qualifier"] = m["rest"]
    if kind == "quantity":
        if m["unit"] is None and past[:1].isalpha():   # 'Qty: 2012  cases': cut-off unit
            return None, "unit_unknown", []
        unit = _unit(m["unit"], spec)
        if unit is None:
            return None, "unit_unknown", []
        return {**out, "unit": unit}, None, []
    currency, problem, basis, stated = _currency(m["pre"], m["post"], declared)
    if currency is None:
        return None, problem, [{"value": out["value"], "currency": code, "basis": source}
                               for code, source in stated]
    out.update(currency=currency, currency_basis=basis)
    if spec.get("per_unit"):
        unit = _unit(m["per"], {}) if m["per"] else None
        if unit is None:
            return None, "unit_unknown", []
        out["per_unit"] = unit
    elif m["per"]:
        return None, "several_values", []   # a rate where an amount is asked
    return out, None, []


def _in_units(spec: Mapping[str, Any], raw: str, convention: str | None
              ) -> dict[str, Any] | None:
    """A quantity stated once per unit ('1509EA; 378CS', parts split by ';' or '/', or one
    part before its separator: '1509EA;', its other unit not read): every
    part a number and its own known unit, the units distinct and one of them the field's
    ``default_unit``; the reading in that unit, the others its ``alternates`` (value,
    unit, ``span`` in ``raw``), never a pick between numbers. None for any other text (it
    stays one reading: a second number is ``several_values``)."""
    default = spec.get("default_unit")
    parts = [m for m in re.finditer(r"[^;/]+", raw) if m.group().strip()]
    if default is None or not parts or (len(parts) < 2 and not re.search(r"[;/]", raw)):
        return None   # one part reads here only with its separator ('1509EA;')
    stated = []
    for part in parts:
        m = _QUANTITY.fullmatch(part.group().strip())
        if m is None or m["approx"] or m["rest"] or m["unit"] is None:
            return None
        number, _ = du.parse_number(m["num"], convention)
        unit = _unit(m["unit"], spec)
        if number is None or unit is None:
            return None
        lead = len(part.group()) - len(part.group().lstrip())
        stated.append({"value": _plain(number), "unit": unit,
                       "span": (part.start() + lead, part.start() + lead +
                                len(part.group().strip()))})
    units = [reading["unit"] for reading in stated]
    if len(set(units)) != len(units) or default not in units:
        return None
    main = stated.pop(units.index(default))
    return {"value": main["value"], "unit": default, **({"alternates": stated} if stated else {})}


def _plain(number: Decimal) -> str:
    text = format(number, "f")
    return "0" if text in ("-0", "+0") else text


# --------------------------------------------------------------------------- extraction

def _cite(handle: str | None, digest: str, page: int | None, text: str, start: int,
          end: int, text_digest: str | None = None) -> dict[str, Any]:
    out = {"l1_handle": handle, "source_sha256": digest, "page": page, "start": start,
           "end": end, "text": text[start:end]}
    if text_digest is not None:
        out.update(text_sha256=text_digest, text_source="ocr")
    return out


def _labelled_values(pattern: re.Pattern[str], pages: list[tuple[int | None, str]],
                     cite: Any, declared: tuple[re.Pattern[str], re.Pattern[str]] | None = None
                     ) -> Iterator[tuple[str, str, dict, str]]:
    """(label, value, citation, past) of every labelled value, in text order; a value cut
    at the next label (:func:`_labelled`) is followed by that label's own read. ``past``
    is the next cell of the line when a column gap ended the value and that cell is no
    label (none of the class's, no ':' or '#'; it may hold the unit or the rest of the
    value), else ''. ``declared`` (:func:`_declared_labels`): a label with no value of
    its own before another declared label ('Supplier:  Phone:'), or on a row of declared
    labels alone ('Lot Number:  Weight:  Qty:', a header over its values), is no read
    (no blank: it names the field, :func:`_seen_labels`); alone on its line it is a
    blank."""
    for page, text in pages:
        offset = 0
        for line in text.splitlines(keepends=True):
            body, at, pos = line.rstrip("\r\n"), 0, 0
            header = declared is not None and declared[1].fullmatch(body) is not None
            while (m := pattern.search(body, pos)) is not None:
                start, end = offset + at + m.start("value"), offset + at + m.end("value")
                value = m["value"]
                if declared is not None and (declared[0].fullmatch(value) or not value and (
                        header or declared[0].match(body, m.end()) is not None)):
                    value = None   # no value beside it: no read
                if not value:   # a label with no value: cite the label
                    start, end = offset + at + m.start("label"), offset + at + m.end("label")
                pos, past = m.end(), ""
                if (gap := du.column_gap_end(body, pos)) is not None and \
                        (cell := _CELL.match(body, gap)) and \
                        pattern.match(body, gap) is None and \
                        not {":", "#"} & set(cell.group()):
                    past = cell.group()
                if value is not None:
                    yield _label_key(m["label"]), value, cite(page, text, start, end), past
                if pos < len(body) and not body[pos].isspace():   # cut at the next label
                    body, at, pos = body[pos:], at + pos, 0
            offset += len(line)


def _checkbox_values(pattern: re.Pattern[str] | None, pages: list[tuple[int | None, str]],
                     cite: Any) -> Iterator[tuple[str, str, dict]]:
    """(label, box, citation) of every checkbox label read (:func:`_checkbox_pattern`),
    citing the box, or the label when no box was read; a label boxed on both sides reads
    as the whole span (never a known mark), citing both boxes."""
    if pattern is None:
        return
    for page, text in pages:
        offset = 0
        for line in text.splitlines(keepends=True):
            for m in pattern.finditer(line.rstrip("\r\n")):
                label, value = ("blabel", "before") if m["blabel"] else ("label", "value")
                start, end = m.span(value)
                if start == end:
                    start, end = m.span(label)
                box = m[value]
                if m["after"]:
                    end = m.end("after")
                    box = line[start:end]
                yield _label_key(m[label]), box, cite(page, text, offset + start,
                                                       offset + end)
            offset += len(line)


# An ``unseparated`` field's label with no ':' or '#' (a printed form's 'Total QTY (EA)'
# box, 'Rejects 0'): a cell start, the label, whitespace, then a number.
_BARE_NUMBER = re.compile(r"~?[ \t]*" + du.NUMBER_TOKEN.pattern + r"\??")
_LABEL_UNIT = re.compile(r"\(([a-z]{1,8})\)$")   # the unit a label states: 'qty (ea)'
_GAP = re.compile(du.COLUMN_GAP)
_CELL_START = r"(?:^|(?<=\t)|(?<=  ))[ \t]*"
# A run of letters (no digit), the text after a label-stated unit that ends its value.
_WORD = re.compile(r"[ \t]+([^\W\d_][^\W\d_.]*)")


@functools.lru_cache(maxsize=16)
def _bare_patterns(profile_id: str
                   ) -> tuple[re.Pattern[str], ...] | None:
    """(at a cell start, after a value, glued, any after a space) patterns of a class's
    ``unseparated`` quantity labels: followed by whitespace and a number, a '.' or ',' allowed before it
    ('Total QTY (EA), 744'); glued: at a cell start and followed, with no space or after
    a mark ('.', ',', ';', '_', '|'), by other text (:func:`_bare_values`); None when it
    has none."""
    labels = [label for spec in _profile(profile_id)["fields"]
              if spec.get("unseparated") and "keyed" not in spec for label in spec["labels"]]
    if not labels:
        return None
    names = r"(?P<label>" + _alternation(labels) + r")"
    label = names + r"[.,]?[ \t]+(?=~?[ \t]*\d)"
    glued = names + r"(?<!\w(?=\w))(?=[.,;_|]|[^\s.,;:_|~\d])"
    return (re.compile(_CELL_START + label, re.IGNORECASE),
            re.compile(r"[ \t]+" + label, re.IGNORECASE),
            re.compile(_CELL_START + glued, re.IGNORECASE),
            re.compile(r"[ \t]+" + names + r"(?!\w)", re.IGNORECASE))


def _bare_values(profile_id: str, pages: list[tuple[int | None, str]], cite: Any,
                 labelled: re.Pattern[str], layout: Container[int | None] = ()
                 ) -> Iterator[tuple[str, str, dict, str, str | None]]:
    """(label, value, citation, past) of every unseparated read (:func:`_bare_patterns`,
    module docstring): the number and what follows it in its cell, so a second number or
    an unknown unit there stays that read's gap; a label stating its unit ('Samples (EA)
    1') reads the number in that unit when words follow. A value is cut where another
    unseparated label and its number follow ('Total QTY (EA) 1502 Samples (EA) 1' is two
    reads); a unit word other than the one its label states is ``unit_unknown``.
    ``past`` as :func:`_labelled_values` (``labelled``: the class's labels); the last item
    is a gap reason the read already has, else None. On a ``layout`` page (word-box OCR,
    :func:`_table_rows`) a label at a cell start with other text glued to it or after a
    mark, up to its cell's end or the next such label ('Total QTY (EA)_|$ QF',
    'Total QTY (EA)lvi 264': handwriting in the label's box the OCR misread), is the gap
    ``unreadable`` citing that text, never a number taken out of it, and so is a fill
    line glued to it with nothing after it in its cell ('Samples (EA)__|', as 'Samples
    (EA): ____' reads); text after a plain space ('Total QTY filled') stays unread (a
    printed word or a reader limit)."""
    patterns = _bare_patterns(profile_id)
    if patterns is None:
        return
    start, follow, glued, after = patterns
    for page, text in pages:
        offset = 0
        for line in text.splitlines(keepends=True):
            body, pos = line.rstrip("\r\n"), 0
            if page in layout:
                for m in glued.finditer(body):
                    if start.match(body, m.start()) is not None:
                        continue
                    gap = _GAP.search(body, m.end())
                    stop = gap.start() if gap else len(body)
                    nxt = after.search(body, m.end(), stop)
                    stop = nxt.start() if nxt else stop
                    junk = re.match(r"[.,;_| \t]*", body[m.end():stop]).end() + m.end()
                    if re.search(r"[^\W_]", body[junk:stop]):
                        yield (_label_key(m["label"]), body[junk:stop].rstrip(),
                               cite(page, text, offset + junk,
                                    offset + junk + len(body[junk:stop].rstrip())),
                               "", "unreadable")
                    elif "_" in body[m.end():junk] and not body[junk:stop].strip():
                        # Only the label's fill line ('Samples (EA)__|'): its box read no
                        # value, as 'Samples (EA): ____' (unreadable, never blank).
                        fill = body[m.end():junk].rstrip()
                        yield (_label_key(m["label"]), fill,
                               cite(page, text, offset + m.end(),
                                    offset + m.end() + len(fill)), "", "unreadable")
            while (m := start.search(body, pos)) is not None:
                while m is not None:
                    at = m.end()
                    number = _BARE_NUMBER.match(body, at)
                    end = number.end() if number else at
                    gap = _GAP.search(body, end)
                    cell_end = gap.start() if gap else len(body)
                    stated = _LABEL_UNIT.search(_label_key(m["label"]))
                    nxt = follow.match(body, end)
                    word = _WORD.match(body, end)
                    past, problem = "", None
                    if stated and nxt is None and word is not None and \
                            _unit(word[1], {}) not in (None, _unit(stated[1], {})):
                        end, raw, problem = word.end(), body[at:word.end()].strip(), \
                            "unit_unknown"
                    elif nxt is not None or (stated and (end == cell_end or (
                            word is not None and _unit(word[1], {}) is None))):
                        raw = body[at:end].strip()
                        if stated and raw:
                            raw = f"{raw} {stated[1]}"
                    else:
                        end = cell_end
                        raw = body[at:end].strip()
                        if stated and raw and not re.search(r"[A-Za-z]", raw):
                            raw = f"{raw} {stated[1]}"
                        if (after := du.column_gap_end(body, end)) is not None and \
                                (cell := _CELL.match(body, after)) and \
                                labelled.match(body, after) is None and \
                                start.match(body, after) is None and \
                                not {":", "#"} & set(cell.group()):
                            past = cell.group()
                    yield _label_key(m["label"]), raw, cite(page, text, offset + at,
                                                             offset + end), past, problem
                    pos, m = end, nxt
            offset += len(line)


@functools.lru_cache(maxsize=16)
def _block_patterns(profile_id: str) -> tuple[tuple[str, re.Pattern[str], re.Pattern[str]],
                                              ...]:
    """(field, label at a cell start, key) of a class's ``unseparated`` keyed fields."""
    out = []
    for spec in _profile(profile_id)["fields"]:
        if spec.get("unseparated") and "keyed" in spec:
            out.append((spec["name"], re.compile(
                _CELL_START + r"(?P<label>" + _alternation(spec["labels"]) + r")\.?(?!\w)",
                re.IGNORECASE), re.compile(r"(?<![\w-])(?:" + profiles()["keys"][spec["keyed"]]
                                           + r")\b", re.IGNORECASE)))
    return tuple(out)


def _keyed_entries(body: str, key: re.Pattern[str], at: int, offset: int, page: int | None,
                   text: str, cite: Any) -> list[tuple[str, dict]]:
    """(entry, citation) of each key of ``body`` from ``at``, one entry per key: the key,
    then its value, the first cell, pipe or ';' part after it ('P-0121 = 07  RM': the next cell
    holds no digit, an initial, never the value); a later part with a digit makes the
    entry its whole text (``several_values``, never a pick)."""
    found = list(key.finditer(body, at))
    out = []
    for index, m in enumerate(found):
        stop = found[index + 1].start() if index + 1 < len(found) else len(body)
        segment = body[m.end():stop]
        lead = re.match(r"[ \t|]*[=:;,-]?[ \t|]*", segment).end()
        parts = [part.strip(" \t,") for part in re.split(r"[;|]|" + du.COLUMN_GAP,
                                                          segment[lead:])]
        parts = [part for part in parts if part]
        value = parts[0] if parts else ""
        end = m.end() + lead + (segment[lead:].find(value) + len(value) if value else 0)
        if any(re.search(r"\d", part) for part in parts[1:]):
            value = " ".join(parts)
            end = m.end() + len(segment.rstrip(" \t;,|"))
        out.append((f"{m.group()} {value}".rstrip(),
                    cite(page, text, offset + m.start(), offset + end)))
    return out


_QTY_COLUMN = re.compile(r"\b(?:qty|quantity|count)\b", re.IGNORECASE)


def _keyed_blocks(profile_id: str, pages: list[tuple[int | None, str]], cite: Any,
                  layout: Container[int | None] = ()
                  ) -> Iterator[tuple[str, str | None, Any]]:
    """(field, entry, citation) of every unseparated keyed read (module docstring): a
    keyed label followed by whitespace and its keys on its line ('DAMAGES P-0121 07'), or
    a keyed label whose line holds no key ('DAMAGES (part code; qty; initial)', 'Damages:')
    heading the lines below it on its page that start with a key (blank lines and one
    line with no digit before the first, a column header naming a quantity column, are
    passed; any other line, a section heading or a list header such as 'Components  Lot',
    ends them). The box is blank, ``(field, '', the label's citation)``, ``value_missing``,
    only when its label's line holds nothing after the label but its printed
    parenthetical (:data:`_PRINTED_BOX`) and its lines end before any key line at the
    next section (a class label or table header: :func:`_box_end`) or the page end; any
    other text there, digits or letters ('see attached', handwriting OCR read as letters),
    leaves the label unread (``no_key``), never blank; a line of letters and marks with
    no three-letter word and no digit is passed (garbled handwriting), and when a key is
    read every such unkeyed part is yielded too (the field's ``key_missing``). On a
    ``layout`` page (:func:`_table_rows`) whose label line's next cell is a class label,
    the box is the label line's cells left of that cell, its lines cut at that column
    only between cells: a cell running across it leaves the box unread, never a key cut
    short. A box left unread is ``(field, None, (page, spans))``: the spans of its text
    (the label line's unkeyed text and the marks below it; the line that left it unread
    only when there is no other: that line may be the next printed section, unknown to
    the profile), what decides whether OCR read it as handwriting (:func:`_fields`). A
    label read with ':' or '#' and a key on its line is :func:`_labelled_values`' read,
    never this one."""
    for name, lead, key in _block_patterns(profile_id):
        for page, text in pages:
            lines, offset = [], 0
            for line in text.splitlines(keepends=True):
                lines.append((offset, line.rstrip("\r\n")))
                offset += len(line)
            if page in layout and lines and lines[-1][1].strip() == du.LAYOUT_MARK:
                lines.pop()   # the layout mark is the page end, not box text
            index = 0
            while index < len(lines):
                offset, body = lines[index]
                index += 1
                m = lead.search(body)
                if m is None:
                    continue
                rest = body[m.end():]
                if key.search(rest):
                    if not re.match(r"[ \t]*[:#]", rest):
                        for entry, citation in _keyed_entries(body, key, m.end(), offset,
                                                              page, text, cite):
                            yield name, entry, citation
                    continue
                label = cite(page, text, offset + m.start("label"), offset + m.end("label"))
                # On a layout page the box stops at the next cell of its line only when
                # that cell is the next section (a class label): handwriting in the box
                # is the label line's next cell itself.
                width = next((a for a, _ in _cells(body) or () if a >= m.end()), None) \
                    if page in layout else None
                if width is not None and not _box_end(profile_id, body[width:]):
                    width = None
                # Blank only when the label's line holds nothing in its box but the
                # printed parenthetical ('(part code; qty; initial)') and marks.
                printed = _PRINTED_BOX.match(body, m.end(), width or len(body)).end()
                blank = printed == len(body[:width])
                # Box text naming no key: unread unless a key is read beside it, then
                # each part is the field's key_missing (never a complete-looking read).
                unkeyed = [] if blank else [(offset + printed, body[printed:width].rstrip())]
                header, taken, ended = False, 0, []
                while index < len(lines):
                    offset, body = lines[index]
                    if width is not None:
                        if any(a < width < b for a, b in _cells(body) or ()):
                            blank = False   # a cell runs out of the box: never cut in it
                            ended = [(offset, body.rstrip())]
                            break
                        body = body[:width]
                    stripped = body.lstrip(" \t|")
                    if not stripped.strip():
                        index += 1
                        continue
                    if key.match(stripped):
                        taken += 1
                        index += 1
                        for entry, citation in _keyed_entries(
                                body, key, len(body) - len(stripped), offset, page, text,
                                cite):
                            yield name, entry, citation
                        continue
                    if not (taken or header or re.search(r"\d", stripped) or
                            lead.search(body)) and _QTY_COLUMN.search(stripped):
                        header = True   # the box's own column header ('Part code  Qty')
                        index += 1
                        continue
                    if lead.search(body) is not None or _box_end(profile_id, stripped):
                        break
                    blank = False
                    if re.search(r"\d|[^\W\d_]{3}", stripped):
                        ended = [(offset + len(body) - len(stripped), stripped.rstrip())]
                        break   # a number or a printed word: the box has ended (unread)
                    # Letters and marks with no word and no digit ('wr', '~ ,.'): OCR of
                    # handwriting in the box; a key below it is still the box's.
                    unkeyed.append((offset + len(body) - len(stripped), stripped.rstrip()))
                    index += 1
                if blank and not taken:
                    yield name, "", label
                elif taken:
                    for at, part in unkeyed:
                        if part:
                            yield name, part, cite(page, text, at, at + len(part))
                else:
                    own = [(at, part) for at, part in unkeyed if part]
                    yield name, None, (page, [(at, at + len(part))
                                              for at, part in own or ended if part])


# What a blank keyed box's label line may hold after the label: its printed
# parenthetical, a colon, fill marks ('DAMAGES (Part Code; QTY; Initial):  ____').
_PRINTED_BOX = re.compile(r"[ \t:._|-]*(?:\([^()\d]*\)[ \t:._|-]*)?")


# What follows a label that starts the next section (:func:`_box_end`).
_SECTION_AFTER_LABEL = re.compile(r"[ \t]*[:#]|\t|  |_{2}|[ \t]*$")


def _box_end(profile_id: str, line: str) -> bool:
    """Whether a line below a keyed box's label is the next section, so the box above it
    is closed: a label of the class at its start ('QC checked', :func:`_seen_pattern`)
    followed by ':', '#', a tab, two spaces, a fill line or the line end, or a table
    header (:func:`_header`). Any other text there is the box's (unread): a label word
    run into text ('PO-121 07', 'Total 3 jugs') may be a handwritten code OCR misread,
    never the box's end (it would read blank)."""
    pattern, _ = _seen_pattern(profile_id)
    m = pattern.match(line)
    if m is not None and _SECTION_AFTER_LABEL.match(line, m.end()):
        return True
    spans = _cells(line) or []
    return len(spans) > 1 and _header(profile_id, [_label_key(line[a:b]) for a, b in spans]
                                      ) is not None


def _stated_reads(reads: list[tuple[dict[str, Any] | None, str | None, dict]]
                  ) -> list[tuple[dict[str, Any] | None, str | None, dict]]:
    """The reads of one field that count: a blank read (``value_missing``, the label
    repeated unfilled) beside a read with a value is none."""
    if any(parsed is not None for parsed, _, _ in reads):
        return [read for read in reads if read[1] != "value_missing"]
    return reads


def _settle(reads: list[tuple[dict[str, Any] | None, str | None, dict]],
            readings: Iterable[dict[str, Any]] = ()
            ) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    """(value, None, []) when every read of a field agrees, else (None, gap reason,
    readings): reads in more than one currency are a ``currency_conflict`` listing each
    (``readings``: those a single read already found in conflict)."""
    values = {json.dumps({k: v for k, v in p.items() if k != "alternates"}, sort_keys=True)
              for p, _, _ in reads if p is not None}
    problems = sorted({problem for _, problem, _ in reads if problem is not None})
    if not reads:
        return None, "not_stated", []
    money = [p for p, _, _ in reads if p is not None and "currency" in p]
    if "currency_conflict" in problems or len({p["currency"] for p in money}) > 1:
        found = [*readings, *({"value": p["value"], "currency": p["currency"],
                               "basis": p["currency_basis"]} for p in money)]
        return None, "currency_conflict", [json.loads(r) for r in dict.fromkeys(
            json.dumps(r, sort_keys=True) for r in found)]
    if len(values) > 1 or (values and problems) or len(problems) > 1:
        return None, "conflicting_values", []
    if problems:
        return None, problems[0], []
    value = json.loads(values.pop())
    alternates: dict[tuple[str, str], list[dict]] = {}
    for parsed, _, _ in reads:
        for reading in parsed.get("alternates", ()):
            alternates.setdefault((reading["unit"], reading["value"]), []).extend(
                reading["citations"])
    if alternates:   # one reading per unit and value, citing each statement of it
        value["alternates"] = [{"value": v, "unit": u, "citations": c}
                               for (u, v), c in sorted(alternates.items())]
        units = [u for u, _ in alternates]
        if (conflicts := sorted({u for u in units if units.count(u) > 1})):
            value["alternate_conflicts"] = conflicts   # another unit's values disagree
    return value, None, []


def _entries(spec: Mapping[str, Any], reads: list[tuple[str, dict, bool]],
             convention: str | None,
             declared: tuple[str, ...], read_by: Callable[[list[dict]], str]
             ) -> tuple[list[dict[str, Any]], list[dict]]:
    """Per-key entries of a keyed field (module docstring) and the cited parts that name
    no key. A read marked whole (:func:`_keyed_blocks`) is one entry, never split on
    ';'. Each entry keeps its own value or gap; nothing is added up."""
    key = re.compile(r"(" + profiles()["keys"][spec["keyed"]] + r")\b[ \t]*[=:-]?[ \t]*",
                     re.IGNORECASE)
    plain = {k: v for k, v in spec.items() if k != "keyed"}
    by_key: dict[str, list[tuple[dict[str, Any] | None, str | None, dict]]] = {}
    unkeyed: list[dict] = []
    for raw, citation, whole in reads:
        at = 0
        for part in [raw] if whole else raw.split(";"):
            lead = len(part) - len(part.lstrip())
            body = part.strip()
            start = citation["start"] + at + lead
            at += len(part) + 1
            if not body:
                continue
            cited = citation if whole else \
                {**citation, "start": start, "end": start + len(body), "text": body}
            m = key.match(body)
            if m is None:
                unkeyed.append(cited)
                continue
            parsed, problem, _ = _parse(plain, body[m.end():].strip(), convention, declared)
            if parsed is not None and "alternates" in parsed:   # cited by the entry
                parsed["alternates"] = [{"value": r["value"], "unit": r["unit"],
                                         "citations": [cited]} for r in parsed["alternates"]]
            by_key.setdefault(m.group(1).upper(), []).append((parsed, problem, cited))
    entries = []
    for name, found in sorted(by_key.items()):
        found = _stated_reads(found)
        value, reason, _ = _settle(found)
        cites = [c for _, _, c in found]
        entries.append({"key": name, **(value or {"value": None, "gap": reason}),
                        "read_by": read_by(cites), "citations": cites})
    return entries, unkeyed


MAX_LINE_ITEMS = 200
# A cell of a column-separated line: words joined by single spaces (a tab or two spaces
# end it); a Markdown rule row ('|---|:---:|') separates a header from its rows.
_CELL = re.compile(r"[^\t ]+(?: [^\t ]+)*")
# A printed column rule inside a gap-split cell: a pipe with a space or the cell's edge
# on both sides ('Time Start | Time Finish', OCR of a ruled form's header); a pipe inside a
# word ('1|2', a misread digit, or '|.') is no rule.
_RULE = re.compile(r"(?:^|(?<= ))\|(?= |$)")


def _ruled(start: int, cell: str) -> list[tuple[int, int]]:
    """The spans of a gap-split cell's pieces between its column rules (:data:`_RULE`),
    each stripped of its spaces, an empty one dropped; every other character is kept."""
    spans, at = [], 0
    for rule in [*_RULE.finditer(cell), None]:
        end = len(cell) if rule is None else rule.start()
        piece = cell[at:end]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            spans.append((start + at + lead, start + at + len(piece.rstrip())))
        at = end + 1
    return spans
_RULE_CELL = re.compile(r"^:?-{3,}:?$")


def _cells(body: str) -> list[tuple[int, int]] | None:
    """Spans of the cells of one line: a Markdown row ('| a |  | c |') split on its pipes
    (an empty cell keeps its place, spanning its pipes so its gap cites the page), any
    other line on tabs or runs of two or more spaces; None for a Markdown rule row."""
    if not body.lstrip().startswith("|"):
        return [span for m in _CELL.finditer(body) for span in _ruled(m.start(), m.group())]
    spans, offset = [], 0
    parts = body.split("|")
    for index, part in enumerate(parts):
        inner = 0 < index < len(parts) - 1 or (index == len(parts) - 1 and part.strip())
        if inner and not part.strip():
            spans.append((offset - 1, min(offset + len(part) + 1, len(body))))
        elif inner:
            lead = len(part) - len(part.lstrip())
            spans.append((offset + lead, offset + lead + len(part.strip())))
        offset += len(part) + 1
    if spans and all(_RULE_CELL.match(body[a:b]) for a, b in spans):
        return None
    return spans


def _cell_text(text: str) -> str:
    """A cell's value: '' for an empty Markdown cell (its span holds only its pipes)."""
    text = text.strip()
    return "" if not text.strip("| \t") else text


def _label_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().rstrip(":.").casefold())


class _Table(NamedTuple):
    """A table header line (:func:`_header`): its number of cells and either the line-item
    column of each cell (``items``) or the cell of each ``cell`` field it addresses
    (``cells``: field -> cell index) with those fields' row labels (``rows``); ``spans``:
    its cells' character spans on its line (:func:`_table_rows`)."""
    width: int
    items: tuple[str, ...] | None
    cells: Mapping[str, int]
    rows: Mapping[str, str]
    spans: tuple[tuple[int, int], ...] = ()


@functools.lru_cache(maxsize=16)
def _item_columns(profile_id: str) -> dict[str, str]:
    """Header label -> line-item column of a class's ``line_items`` (none without)."""
    return {_label_key(label): column["name"]
            for column in (_profile(profile_id).get("line_items") or {}).get("columns", ())
            for label in column["labels"]}


def _header(profile_id: str, keys: list[str]) -> _Table | None:
    """The table a line of two or more cells (their :func:`_label_key` texts) heads, if
    any: a line-items header when every cell is the label of a distinct line-item column;
    else a cell header when it names a column of a ``cell`` field after its first cell
    (the first of the field's columns it names, in declared order). One header is one
    table of one kind, so no cell is read as both an item and a field."""
    columns = _item_columns(profile_id)
    names = [columns.get(key) for key in keys]
    if columns and None not in names and len(set(names)) == len(names):
        return _Table(len(keys), tuple(names), {}, {})
    rows, heads = _cell_addresses(profile_id)
    found = {name: index for name, wanted in heads.items()
             for index in [next((keys.index(head, 1) for head in wanted
                                 if head in keys[1:]), None)]
             if index is not None}
    if not found:
        return None
    return _Table(len(keys), None, found,
                  {row: name for row, name in rows.items() if name in found})


# (header, row field, cell citations, row citation, choice): ``choice`` marks a row whose
# first cell lists the field's row label as one option of several (:func:`_choice_of`); a
# layout row's cell citations hold None for a header cell no row cell was placed under.
_TableRow = tuple[_Table, "str | None", "list[dict | None] | None", dict, bool]
# A label written as one option of a list ('Filling/Setup/Tear down'): which option the
# row is, a mark on the paper (underlined, circled), is not in the text.
# A row label followed by another option: '/Setup', or two or more ' | '-ruled options
# ('Filling | Setup | Tear down', a printed task list OCR reads with its rules).
_OPTION = r"[^\w\s|]?[^\W\d_](?:[^\W\d_]| (?=[^\W\d_]))*"
_CHOICE = re.compile(r"[ \t]*/[ \t]*[^\W\d_]|(?:[ \t]*\|[ \t]*" + _OPTION +
                     r"){2,}(?=[ \t]*\||[ \t]{2}|[ \t]*$)", re.MULTILINE)


def _choice_of(key: str, rows: Mapping[str, str]) -> str | None:
    """The field a row's first gap-split cell (folded, its column rules kept: a ruled
    option list is one choice, never cells) names as one option of a list, if one: a '/'
    list ('filling/setup') or a '|' list of three or more options ('filling |setup|tear
    down', a ruled task list; :data:`_CHOICE`); a speck before an option is kept in it."""
    for rule, least in (("/", 2), ("|", 3)):
        parts = [part.strip(" \t") for part in key.split(rule)]
        named = {rows[part] for part in parts if part in rows}
        if len(parts) >= least and len(named) == 1:
            return named.pop()
    return None


def _under(span: tuple[int, int], spans: Iterable[tuple[int, int]]) -> list[int]:
    """The indexes of the header cells (``spans``) a row cell's columns overlap."""
    return [index for index, (a, b) in enumerate(spans) if span[0] < b and a < span[1]]


def _placed(spans: list[tuple[int, int]], table: _Table, column: int
            ) -> tuple[int, int] | None:
    """The row cell read for header cell ``column`` by its columns (an OCR layout row,
    :func:`_table_rows`): the one cell after the row label under that header cell and
    under no other; None when no cell or more than one is (never a pick)."""
    found = [span for span in spans[1:] if _under(span, table.spans) == [column]]
    return found[0] if len(found) == 1 and \
        not any(column in _under(span, table.spans) for span in spans[1:]
                if span != found[0]) else None


def _table_rows(profile_id: str, pages: list[tuple[int | None, str]], cite: Any,
                layout: Container[int | None] = ()) -> Iterator[_TableRow]:
    """The one table scanner of a class, for its line items and its ``cell`` fields alike:
    (header, row field, cell citations, row citation) of each row below a header line
    (:func:`_header`; cells split by :func:`_cells`). A row with the header's number of
    cells, one of them not blank, gives each cell's citation, its own span; a row whose
    first cell is a row label of a field the header addresses gives that field, its cells
    None when they do not line up with the header (never fitted to columns). On a
    ``layout`` page (OCR text rebuilt from word boxes, whose character columns are the
    scan's: ``integrations.document_understanding.ocr_layout_text``) such a row with
    another number of cells gives each header cell the one row cell under it alone
    (:func:`_placed`; None for a header cell with none or several), and a row of a cell
    table whose every cell lies under one header cell keeps the table (a task row whose
    cells are blank, a handwritten cell off its row's line). Any other row ends the
    table, given first with no cells when it has two or more cells (``unaligned``), but a
    blank Markdown row keeps a cell table (it reads labelled rows only); a line with no
    cell or the page end ends it too, and a Markdown rule row keeps it."""
    for page, text in pages:
        table: _Table | None = None
        offset = 0
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            spans = _cells(body)
            if spans is not None:
                keys = [_label_key(body[a:b]) for a, b in spans]
                found = _header(profile_id, keys) if len(keys) > 1 else None
                if found is not None:
                    table = found._replace(spans=tuple(spans))
                elif table is not None:
                    field = table.rows.get(keys[0]) if keys else None
                    first = next((_label_key(m.group()) for m in _CELL.finditer(body)
                                  if spans and m.end() >= spans[0][1]), "")
                    choice = (field is not None and _CHOICE.match(body, spans[0][1])
                              is not None) or (field is None and bool(first) and (
                                  field := _choice_of(first, table.rows)) is not None)
                    row = cite(page, text, offset, offset + len(body))
                    aligned = len(spans) == table.width
                    columns = page in layout and not aligned and bool(spans)
                    if choice:
                        yield table, field, None, row, True
                    elif aligned and any(_cell_text(body[a:b]) for a, b in spans):
                        yield table, field, [cite(page, text, offset + a, offset + b)
                                             for a, b in spans], row, False
                    elif field is not None and columns:
                        placed = [_placed(spans, table, index) for index in
                                  range(table.width)]
                        yield table, field, [None if span is None else cite(
                            page, text, offset + span[0], offset + span[1])
                            for span in placed], row, False
                    elif field is not None:
                        yield table, field, None, row, False
                    elif columns and table.items is None and all(
                            len(under := _under(span, table.spans)) == 1 or
                            (not under and span[0] >= table.spans[-1][1])
                            for span in spans):
                        pass
                    elif not (aligned and table.items is None):
                        if len(spans) > 1:
                            yield table, None, None, row, False
                        table = None
            offset += len(line)


def _line_items(spec: Mapping[str, Any], rows: list[_TableRow], convention: str | None,
                declared: tuple[str, ...], read_by: Callable[[list[dict]], str]
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Line items of a profile's table (``line_items``) and their gaps, from the rows of
    its line-items headers (:func:`_table_rows`): each row of as many cells as its header
    is one item, a row that ends the table with two or more cells the gap ``unaligned``
    (its cells are never fitted to columns). Each cell is parsed by its column's kind and
    cited by its own span; a blank or unreadable cell is that cell's gap, never zero and
    never a neighbour's value."""
    columns = {column["name"]: column for column in spec["columns"]}
    items: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for table, _, cells, row, _ in rows:
        if table.items is None:
            continue
        if cells is None:
            gaps.append({"field": "line_items", "reason": "unaligned", "citations": [row]})
        elif len(items) == MAX_LINE_ITEMS:
            gaps.append({"field": "line_items", "reason": "too_long", "citations": [row]})
            break
        else:
            cited = dict(zip(table.items, cells))
            item = _line_item(columns, {n: _cell_text(c["text"]) for n, c in cited.items()},
                              cited, convention, declared)
            item = {"row": len(items) + 1, "cells": item,
                    "read_by": read_by(list(cited.values()))}
            items.append(item)
            gaps.extend({"field": "line_items", "row": item["row"], "column": name,
                         "reason": cell["gap"], "citations": [cell["citation"]]}
                        for name, cell in item["cells"].items() if cell.get("gap"))
    return items, gaps


def _line_item(columns: Mapping[str, Mapping[str, Any]], raw: Mapping[str, str],
               cited: Mapping[str, dict], convention: str | None, declared: tuple[str, ...]
               ) -> dict[str, dict[str, Any]]:
    """The typed cells of one table row: text, dates and codes first, then quantities
    (a bare number takes the row's ``unit_from`` cell as its unit), then money (a
    ``per_unit_of`` price without its own unit is per the row's quantity unit)."""
    rank = {"quantity": 1, "money": 2}
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(raw, key=lambda n: rank.get(columns[n]["kind"], 0)):
        column, value = columns[name], raw[name].strip()
        parse_spec: Mapping[str, Any] = column
        borrowed = {}   # the cell a unit was read from, named on the value
        if column.get("unit_from") and value and not re.search(r"[A-Za-z]", value):
            unit = raw.get(column["unit_from"], "").strip()
            if unit:
                value, borrowed = f"{value} {unit}", {"unit_from": column["unit_from"]}
        if column.get("per_unit_of"):
            parse_spec = {"kind": "money", "per_unit": True}
            unit = (out.get(column["per_unit_of"]) or {}).get("unit")
            if value and "/" not in value and unit:
                value, borrowed = f"{value}/{unit}", {"per_unit_from": column["per_unit_of"]}
        parsed, problem, readings = _parse(parse_spec, value, convention, declared,
                                           cell=True)
        cell = {**parsed, **borrowed} if parsed is not None else \
            {"value": None, "gap": problem}
        if readings:
            cell["readings"] = readings
        out[name] = {**cell, "citation": cited[name]}
    return {name: out[name] for name in raw}


@functools.lru_cache(maxsize=16)
def _cell_addresses(profile_id: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Row label -> field (through the one label-to-field map) and field -> column headers
    (in declared order) of a class's ``cell`` fields."""
    owner, fields = _owner(profile_id), _profile(profile_id)["fields"]
    rows = {_label_key(row): owner[_label_key(row)] for spec in fields
            for row in (spec.get("cell") or {}).get("rows", ())}
    columns = {spec["name"]: tuple(map(_label_key, spec["cell"]["columns"]))
               for spec in fields if "cell" in spec}
    return rows, columns


def _cell_reads(rows: list[_TableRow]
                ) -> Iterator[tuple[str, str | None, dict, str | None]]:
    """(field, cell text, citation, gap) of every table row a ``cell`` field's row label
    starts below a header naming its column (:func:`_table_rows`): the cell in that
    column, cited by its own span; (field, None, the row's citation, gap) when the row
    does not line up (``unaligned``: also a layout row with no cell placed under that
    column, never blank) or names the label as one option of a list (``unreadable``: the
    mark that picks the option is not in the text)."""
    for table, field, cells, row, choice in rows:
        if field is None:
            continue
        cell = None if cells is None else cells[table.cells[field]]
        if cell is None:
            yield field, None, row, "unreadable" if choice else "unaligned"
        else:
            yield field, _cell_text(cell["text"]), cell, None


@functools.lru_cache(maxsize=16)
def _seen_pattern(profile_id: str) -> tuple[re.Pattern[str], dict[str, str]]:
    """Where a document names a field: one of its labels starting a cell (a line, a tab,
    two spaces or a pipe before it) and ending a word (a label ending in ')' may run into
    its fill line: 'Total QTY (EA)____'), a row label alike (a single-spaced
    OCR row 'Total 2012' names the field even though no cell can be read from it); and
    the one label-to-field map each is looked up in."""
    owner = _owner(profile_id)
    rows, _ = _cell_addresses(profile_id)
    labels = set(owner) - set(rows)
    row = r"|(?P<row>" + _alternation(rows) + r")(?!(?<=\w)\w)" if rows else ""
    return re.compile(r"(?:^|(?<=\t)|(?<=  )|(?<=\|))[ \t]*(?:(?P<label>" +
                      _alternation(labels) + r")\.?(?!(?<=\w)\w)" + row + ")",
                      re.IGNORECASE | re.MULTILINE), owner


def _seen_labels(profile_id: str, pages: list[tuple[int | None, str]], cite: Any
                 ) -> dict[str, list[tuple[dict, bool, str]]]:
    """Field -> (citation, choice, why unread) of where the document names it
    (:func:`_seen_pattern`); ``choice``: a cell field's row label is one option of a '/'
    list there ('Filling/Setup'); a plain label before a '/' is never a choice. Why no
    value is read there, should none be (``UNREAD_REASONS``): ``no_key`` (a keyed field:
    no key read in its box), ``single_spaced`` (the label's cell runs on past it, one
    space before the next word: 'Total 2012', 'Rejects none'), ``row_unaligned`` (a row
    label alone in its cell below a header naming the field's column on its page, where
    the row did not line up or the table had ended), ``header_unseen`` (a row label alone
    in its cell with no such header above it: the header line was not read as cells) or
    ``value_unplaced`` (a plain label alone in its cell, its value not beside it)."""
    pattern, owner = _seen_pattern(profile_id)
    specs = {spec["name"]: spec for spec in _profile(profile_id)["fields"]}
    out: dict[str, list[tuple[dict, bool, str]]] = {}
    for page, text in pages:
        headers, offset = [], 0
        for line in text.splitlines(keepends=True):
            spans = _cells(line.rstrip("\r\n")) or []
            if len(spans) > 1 and (table := _header(profile_id, [
                    _label_key(line[a:b]) for a, b in spans])) is not None:
                headers.append((offset, table))
            offset += len(line)
        for m in pattern.finditer(text):
            group = "label" if m["label"] else "row"
            name = owner[_label_key(m[group])]
            # Only a cell field's row label is a pick among options ('Filling/Setup');
            # elsewhere 'Date/Time' or 'Cartons/Pallets' is a label the reader missed.
            choice = group == "row" and _CHOICE.match(text, m.end(group)) is not None
            if "keyed" in specs[name]:
                why = "no_key"
            elif re.match(r" [^\s]", text[m.end(group):m.end(group) + 2]):
                why = "single_spaced"
            elif group == "label":
                why = "value_unplaced"
            elif any(at < m.start() and name in table.cells for at, table in headers):
                why = "row_unaligned"
            else:
                why = "header_unseen"
            out.setdefault(name, []).append((cite(page, text, *m.span(group)), choice, why))
    return out


def extract_fields(content: bytes, mime_type: str, *, filename: str = "",
                   l1_handle: str | None = None, local_pdf_ocr: bool = False,
                   text: str | None = None, issued: str | None = None) -> dict[str, Any]:
    """The class, typed fields, gaps and references of one document's bytes (module
    docstring); ``text``: the capture's OCR text of those bytes (a scan without a text
    layer), read instead of the bytes and cited by its own SHA-256; ``issued``: the
    provenance signal of the file holding them (``ISSUED_SIGNALS``, *Direction*).
    Deterministic for the same bytes, text, MIME type, provenance, profile file and
    extractor; raises ``ValidationError`` only for bytes over the extractor's bounds."""
    return _fields(read_text(content, mime_type, filename=filename, text=text,
                             local_pdf_ocr=local_pdf_ocr), l1_handle, issued)


# A cue running on into a sentence: two lowercase words follow it ("packing slip showing
# our ..."), not a number or a label ("Packing list no. 55").
_PROSE = re.compile(r"[ \t]+[a-z]+[ \t]+[a-z]+\b")
# A prose cue is still a title when its line ends in a number or identifier ("Packing list
# for order 4500") or, above every other class's title line, is no sentence ("Order
# confirmation of your purchase order"); a line ending a sentence is a note.
_ENDS_IN_NUMBER = re.compile(r"\w*\d[\w/-]*[ \t]*$")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*[ \t]*$")
# A label's colon ("Tel: ...", "Ship to:"): a word, then ':' and a space or the line end
# (never a URL's or a time's colon).
_LABEL_COLON = re.compile(r"(?<=[^\W\d_])\.?[ \t]*:(?=[ \t]|$)")
# The provenance signals that a captured file is the own party's (module docstring,
# *Direction*), and the reason a class declaring ``not_issued_by_own_party`` is
# dismissed for each; ``own_issued``: the document itself names an own party as issuer.
ISSUED_SIGNALS = ("issued_category", "own_mailbox_send")
OWN_ISSUED = "own_issued"
NOT_STATED_IN_READ_PAGES = "not_stated_in_read_pages"
LABEL_SEEN_UNREAD = "label_seen_unread"
# Why a seen label was left unread (its gap's ``unread``; :func:`_seen_labels`).
HANDWRITTEN_UNKEYED = "handwritten_unkeyed"
# The share of an unread box's text (its words' characters) OCR must have read below the
# handwriting bound for the box to be handwriting: a stray glyph or mark in handwriting
# may read high, printed text rarely reads low (decision
# docs/decisions/2026-10-19-ao-handwritten-reader-limit.md).
HANDWRITTEN_SHARE = Fraction(3, 4)
UNREAD_REASONS = ("header_unseen", "row_unaligned", "single_spaced", "no_key",
                  HANDWRITTEN_UNKEYED, "value_unplaced")
SPLIT_AT_GAP = "split_at_gap"
MAX_SEEN_CITATIONS = 3
ISSUER_UNKNOWN = "issuer_unknown"


def _domain(address: Any) -> str | None:
    if not isinstance(address, str) or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().casefold() or None


def _when(value: Any) -> datetime | None:
    """A message time (ISO 8601, ``Z`` or an offset) as an aware datetime, else None."""
    try:
        found = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else None


Received = Mapping[tuple[str, str], list["datetime | None"]]


def copy_mailbox(row: Mapping[str, Any]) -> str:
    """The captured mailbox a mail attachment row's copy of its bytes is held in (its
    ``mailbox_upn``, case-folded): the key :func:`_received` tells a forward by."""
    return str(row.get("mailbox_upn")).strip().casefold()


def _received(rows: Iterable[Mapping[str, Any]]) -> Received:
    """Per (mailbox, bytes' SHA-256), when the captured mailbox received an attachment of
    those bytes from outside the own party (its message's ``message_received_at``, None
    when not known): its message's sender (``message_from``) has a domain no captured
    mailbox (``mailbox_upn``) has. That mailbox's send of the same bytes after it is a
    forward or a reply of the received document (module docstring, *Direction*);
    another mailbox's copy never decides it (no reader learns what a mailbox it may not
    read received)."""
    rows = list(rows)
    own = {_domain(row.get("mailbox_upn")) for row in rows} - {None}
    out: dict[tuple[str, str], list[datetime | None]] = {}
    for row in rows:
        if isinstance(row.get("content_sha256"), str) and \
                _domain(row.get("message_from")) not in own | {None}:
            out.setdefault((copy_mailbox(row), row["content_sha256"]), []).append(
                _when(row.get("message_received_at")))
    return out


def _forwarded(row: Mapping[str, Any], received: Received | None) -> bool:
    """Whether the mailbox's send ``row`` forwards or replies with bytes it received
    from outside before the send (either time unknown: it did)."""
    sent = _when(row.get("message_sent_at"))
    return any(at is None or sent is None or at < sent for at in (received or {}).get(
        (copy_mailbox(row), row.get("content_sha256")), ()))


def _issued_by(row: Mapping[str, Any], received: Received | None = None) -> str | None:
    """The provenance signal (``ISSUED_SIGNALS``) that the captured file ``row`` was issued
    by the own party, else None: ``issued_category``, the Portal's DocumentCategory
    captured for this very version names a purchase order the Portal issued to a supplier
    (``operating/doc_metadata_taxonomy.py`` issued records under the supplier root);
    ``own_mailbox_send``, an attachment of a message whose sender is the captured mailbox
    itself (``message_from`` equal to ``mailbox_upn``), unless that mailbox received the
    same bytes from outside before it sent them (``received``, :func:`_received`,
    :func:`_forwarded`: the send forwards or replies with a received document). Never a
    name or a number shape."""
    from operating.doc_metadata_taxonomy import ISSUED_RECORD_CATEGORIES, confirmed_category

    category = confirmed_category(row)
    if category is not None and (ISSUED_RECORD_CATEGORIES.get(category) or {}).get(
            "root") == "P":
        return "issued_category"
    sender, mailbox = row.get("message_from"), row.get("mailbox_upn")
    if isinstance(sender, str) and isinstance(mailbox, str) and sender.strip() and \
            sender.strip().casefold() == mailbox.strip().casefold() and \
            not _forwarded(row, received):
        return "own_mailbox_send"
    return None


def _classes(pages: list[tuple[int | None, str]], cite: Callable[..., Any],
             issued: str | None = None) -> tuple[dict[str, list[Any]], dict[str, str]]:
    """The title cues (``cite``-d) of each class that counts (module docstring, *Class*),
    and why each other class whose title cue the text holds does not:
    ``mention_beside_title`` (each cue of it runs on into prose and another class has a
    title line, and none is a prose title, or each is prose beside a title line the
    direction rule dismissed), ``after_other_title`` (a whole-line class's title after
    another class's title that is no prose title), ``beside_other_title`` (only numbered
    titles, beside another class's title line), ``numbered_title_only`` (only numbered
    titles, none the first line of its page or no other field of the class stated),
    ``beside_numbered_title`` (only
    whole-line titles, its number never stated, below a title of a class whose number
    the document states), ``own_issued`` (the document's issuing party is an own party:
    :func:`_own_issued`) or the provenance signal ``issued`` (``ISSUED_SIGNALS``) for a
    class declaring ``not_issued_by_own_party``."""
    alone = {p["id"] for p in profiles()["profiles"] if p.get("title_alone")}
    # Per class: (position, title line, prose, the cue's line from the cue on, citation,
    # numbered); a numbered title ("Purchase Order: 4500") is a title line here.
    cues: dict[str, list[tuple[tuple[int, int], bool, bool, str, Any, bool]]] = {}
    heads: dict[str, list[tuple[tuple[int, int], str]]] = {}   # each cue's page before it
    for numbered, patterns in ((False, _title_patterns()), (True, _numbered_title_patterns())):
        for profile_id, pattern in patterns:
            for page, page_text in pages:
                for m in pattern.finditer(page_text):
                    start = m.start() + len(m.group()) - len(m.group().lstrip(" \t"))
                    end = m.start() + len(m.group().rstrip(" \t"))
                    rest = page_text[end:].split("\n", 1)[0]
                    heads.setdefault(profile_id, []).append(((page or 0, start),
                                                             page_text[:start]))
                    cues.setdefault(profile_id, []).append(
                        ((page or 0, start), numbered or not rest.strip(),
                         not numbered and _PROSE.match(rest) is not None,
                         page_text[start:end] + rest, cite(page, page_text, start, end),
                         numbered))
    for found in cues.values():
        found.sort(key=lambda c: c[0])
    head = {p: min(at)[1] for p, at in heads.items()}   # before each class's first cue
    titles = {p: [c[0] for c in found if c[1]] for p, found in cues.items()}
    dismissed: dict[str, str] = {}
    for profile_id in sorted(set(cues) - alone):
        other = [at for p, ats in titles.items() if p != profile_id for at in ats]
        kept = [c for c in cues[profile_id] if not c[2] or not other or (
            not _SENTENCE_END.search(c[3])
            and (_ENDS_IN_NUMBER.search(c[3]) is not None or c[0] < min(other)))]
        if kept:
            cues[profile_id] = kept
        else:
            del cues[profile_id]
            dismissed[profile_id] = "mention_beside_title"
    # A whole-line class's title after another class's title is a mention of it; a prose
    # title above it is not that title, so both stay (the document is ambiguous).
    first = [c[0] for p, found in cues.items() if p not in alone for c in found if not c[2]]
    for profile_id in sorted(alone & set(cues)):
        if first and min(c[0] for c in cues[profile_id]) > min(first):
            del cues[profile_id]
            dismissed[profile_id] = "after_other_title"
    # A numbered title is a reference beside another class's title line, wherever it
    # stands; a title line of a class whose number the document never states is a
    # heading below a title of a class whose number it states.
    lines = {p for p, found in cues.items() if any(not c[2] and not c[5] for c in found)}
    for profile_id in sorted(set(cues)):
        if all(c[5] for c in cues[profile_id]) and lines - {profile_id}:
            del cues[profile_id]
            dismissed[profile_id] = "beside_other_title"
    # Alone, a numbered title names that document ("Purchase Order: 4500" in a note, a
    # delivery note or a remittance advice) unless it heads its page (its first line)
    # and the document states another field of its class.
    by_id = {p["id"]: p for p in profiles()["profiles"]}
    for profile_id in sorted(set(cues)):
        before = dict(heads[profile_id])
        if all(c[5] for c in cues[profile_id]) and not (
                any(not before[c[0]].strip() for c in cues[profile_id])
                and _stated(by_id[profile_id], pages) - {by_id[profile_id]["number_field"]}):
            del cues[profile_id]
            dismissed[profile_id] = "numbered_title_only"
    stating = {p: min(c[0] for c in found if not c[2]) for p, found in cues.items()
               if any(not c[2] for c in found)
               and by_id[p].get("number_field") in _stated(by_id[p], pages)}
    for profile_id in sorted(set(cues) - set(stating)):
        if all(c[1] and not c[5] for c in cues[profile_id]) and any(
                at < min(c[0] for c in cues[profile_id]) for at in stating.values()):
            del cues[profile_id]
            dismissed[profile_id] = "beside_numbered_title"
    titled = False   # a title line the direction rule dismissed
    for profile in profiles()["profiles"]:
        if profile["id"] in cues and "not_issued_by_own_party" in profile:
            reason = issued or (OWN_ISSUED if _own_issued(
                profile, head[profile["id"]], pages) else None)
            if reason:
                titled = titled or any(c[1] for c in cues[profile["id"]])
                dismissed[profile["id"]] = reason
                del cues[profile["id"]]
    # Beside that title a prose title is still a mention ("Packing slip must reference
    # PO100771" in the terms of a PO the own party issued), never the class left over.
    for profile_id in sorted(cues) if titled else ():
        if all(c[2] for c in cues[profile_id]):
            del cues[profile_id]
            dismissed[profile_id] = "mention_beside_title"
    return {p: [c[4] for c in found] for p, found in cues.items()}, dismissed


def _letterhead(head: str, party: re.Pattern[str] | None) -> str:
    """The letterhead block of ``head`` (the text of the title's page before the title):
    its unlabelled text up to the first line or column starting with a party-role word
    (``party``: :func:`_party_line_pattern`; "Vendor PLX Manufacturing", "Ship To",
    "To: PLX ...") or a label with no value ("Ship to:"). A labelled value ("Tel: ...",
    "Date: ...") is never letterhead text but does not end the block."""
    found = party.search(head) if party else None
    out = []
    for line in head[:found.start() if found else len(head)].split("\n"):
        colon = _LABEL_COLON.search(line)
        if colon is None:
            out.append(line)
            continue
        out.append(line[:colon.start()])
        if not line[colon.end():].strip():
            break
    return "\n".join(out)


def _own_issued(profile: Mapping[str, Any], head: str,
                pages: list[tuple[int | None, str]]) -> bool:
    """Whether the issuing party on the document is an own party (the profile file's
    ``own_parties``), in no recipient field's value (a customer's PO naming PLX as its
    vendor above its title stays a customer's PO): named in an issuer field's value, or,
    when no issuer field is stated, in its letterhead block (:func:`_letterhead`: a
    party-role line names another party, never the issuer, so "To: PLX Manufacturing",
    "Vendor PLX Manufacturing" or "To" above PLX's address above a customer's title
    addresses PLX). An issuer field naming another party decides: the PLX block above
    the title is then the addressee. An issuer value read after ``#`` ("Customer #
    C-12345" on a PO PLX printed) is a number, never a party; a recipient field's
    ("Vendor #: PLX Manufacturing") still names its party."""
    rule, own = profile["not_issued_by_own_party"], _own_party_pattern()
    pattern, owner = _label_pattern(profile["id"])
    letterhead = _letterhead(head, _party_line_pattern(profile["id"]))
    recipients = rule.get("recipient_fields", ())
    named: dict[str, bool] = {}
    for label, value, before, _ in _labelled_values(
            pattern, pages, lambda page, text, start, end: text[:start]):
        name = owner[label]
        if value.strip() and (name in recipients
                              or not re.search(r"#[ \t]*:?[ \t]*$", before)):
            named[name] = named.get(name, False) or own.search(value) is not None
    if any(named.get(name) for name in recipients):
        return False
    issuers = [named[name] for name in rule["issuer_fields"] if name in named]
    return any(issuers) if issuers else own.search(letterhead) is not None


def _fields(read: CapturedText, l1_handle: str | None, issued: str | None = None
            ) -> dict[str, Any]:
    """:func:`extract_fields` of one reading of a document
    (``integrations.captured_text``) whose file has the provenance signal ``issued``."""
    digest, text_digest = read.source_sha256, read.text_sha256
    status, method, mime, ocr = read.status, read.method, read.mime_type, read.ocr_pages
    # The column rule (:func:`_table_rows`) reads only OCR pages whose text says it is
    # word-box layout; other OCR text keeps the count rule (a dropped cell shifts it). Its
    # mark line is read bare; the spans it names are the page's words OCR read below the
    # handwriting bound (``low``).
    pages, low = [], {}
    for page, text in read.pages:
        if page in ocr and du.layout_page(text):
            text, low[page] = du.layout_confidence(text)
        pages.append((page, text))
    layout = frozenset(low)

    def cite(page: int | None, page_text: str, start: int, end: int) -> dict[str, Any]:
        return _cite(l1_handle, digest, page, page_text, start, end, text_digest)

    def read_by(citations: list[dict]) -> str:
        return "ocr" if any(c["page"] in ocr for c in citations) else "parser"

    result: dict[str, Any] = {
        "format": FORMAT, "extractor": extractor_version(), "source_sha256": digest,
        "l1_handle": l1_handle, "mime_type": mime, "method": method,
        "tier": TIER, "machine_read": True, "external_assertion": True,
        "status": status, "profile": None, "object_type": None,
        "class_evidence": [], "fields": {}, "gaps": [], "references": []}
    if text_digest is not None:
        result.update(text_sha256=text_digest, text_source="ocr")
    skipped = read.skipped_by_cap
    if skipped is not None:   # pages past the page bound: the reading is not the whole file
        result["skipped_by_cap"] = skipped
        result["gaps"].append({"field": "pages", "reason": "skipped_by_cap", **skipped,
                               "citations": []})
    unstated = "not_stated" if skipped is None else NOT_STATED_IN_READ_PAGES
    if status != "extracted":
        return result
    classes, dismissed = _classes(pages, cite, issued)
    if len(classes) != 1:
        own = not classes and any(reason in (OWN_ISSUED, *ISSUED_SIGNALS)
                                  for reason in dismissed.values())
        result["status"] = ("ambiguous_class" if classes else OWN_ISSUED if own
                            else "unclassified")
        result["class_evidence"] = [{"profile": p, "citations": c[:3]}
                                    for p, c in sorted(classes.items())]
        if classes:   # the document names several types: a typed gap, never one of them
            result["gaps"].append({"field": "class", "reason": "classes_disagree",
                                   "citations": [c[0] for _, c in sorted(classes.items())]})
        return result
    (profile_id, cues), = classes.items()
    profile = next(p for p in profiles()["profiles"] if p["id"] == profile_id)
    result.update(status="classified", profile=profile_id, object_type=profile["object_type"],
                  class_evidence=[{"profile": profile_id, "citations": cues[:3]}])
    if "not_issued_by_own_party" in profile:   # no signal of who issued it: a doubt shown
        result["gaps"].append({"field": "issuer", "reason": ISSUER_UNKNOWN, "citations": []})
    convention = du.number_convention("\n".join(t for _, t in pages))
    declarations = [read[:3] for read in _labelled_values(_declaration_pattern(), pages, cite)]
    declared = tuple(sorted({code for _, value, _ in declarations
                             if (code := currency_reading(value)[0]) is not None}))
    pattern, owner = _label_pattern(profile_id)
    specs = {spec["name"]: spec for spec in profile["fields"]}
    seen: dict[str, list[tuple[dict[str, Any] | None, str | None, dict]]] = {}
    readings: dict[str, list[dict[str, Any]]] = {}
    raws: dict[str, list[tuple[str, dict, bool]]] = {}
    table_rows = list(_table_rows(profile_id, pages, cite, layout))
    in_rows = [(row["page"], row["start"], row["end"])
               for _, field, _, row, _ in table_rows if field is not None]
    # An unseparated read on a field's table row is the table's (its cell or its gap).
    bare = [read for read in _bare_values(profile_id, pages, cite, pattern, layout)
            if not any(page == read[2]["page"] and start <= read[2]["start"] < end
                       for page, start, end in in_rows)]
    reads = [(owner[label], raw, citation, False, past, problem)
             for label, raw, citation, past, problem in [
                 *((*read, None) for read in _labelled_values(
                     pattern, pages, cite, _declared_labels(profile_id))),
                 *((*read, "", None) for read in _checkbox_values(
                     _checkbox_pattern(profile_id), pages, cite)),
                 *bare]]
    reads += [(name, raw, citation, True, "", problem) for name, raw, citation, problem in
              _cell_reads(table_rows)]
    boxes: dict[str, list[tuple[int | None, list[tuple[int, int]]]]] = {}
    for name, entry, citation in _keyed_blocks(profile_id, pages, cite, layout):
        if entry is None:   # a box left unread: its text's spans
            boxes.setdefault(name, []).append(citation)
        else:
            raws.setdefault(name, []).append((entry, citation, True))
    texts = dict(pages)

    def handwritten(name: str) -> list[dict]:
        # The citations of a keyed field's unread boxes when OCR read at least
        # HANDWRITTEN_SHARE of each box's text in words below the handwriting bound (a
        # reader limit of its own, never a key read out of it), else none: a box on a
        # page without word confidences, or mostly read at or above the bound (print: a
        # note, a code), stays no_key.
        out = []
        for page, spans in boxes.get(name, ()):
            words = [w.span() for a, b in spans
                     for w in re.compile(r"\S+").finditer(texts[page], a, b)]
            read_low = sum(b - a for a, b in words
                           if any(x <= a and b <= y for x, y in low.get(page, ())))
            if not words or read_low < HANDWRITTEN_SHARE * sum(b - a for a, b in words):
                return []
            out += [cite(page, texts[page], a, b) for a, b in spans]
        return out
    for name, raw, citation, cell, past, problem in reads:
        spec = specs[name]
        if "keyed" in spec:
            raws.setdefault(name, []).append((raw, citation, False))
            continue
        parsed, problem, stated = (None, problem, []) if raw is None or problem else \
            _parse(spec, raw, convention, declared, cell=cell, past=past)
        if parsed is not None and "alternates" in parsed:   # each cited by its own span
            parsed["alternates"] = [
                {"value": r["value"], "unit": r["unit"], "citations": [
                    {**citation, "start": citation["start"] + r["span"][0],
                     "end": citation["start"] + r["span"][1],
                     "text": raw[r["span"][0]:r["span"][1]]}]
                    if citation["text"] == raw else [citation]}
                for r in parsed["alternates"]]
        if parsed is not None and parsed.get("currency_basis") == "declared":
            parsed["currency_citations"] = [c for _, value, c in declarations
                                            if value.strip().upper() == parsed["currency"]]
        seen.setdefault(spec["name"], []).append((parsed, problem, citation))
        readings.setdefault(spec["name"], []).extend(stated)
    named = _seen_labels(profile_id, pages, cite)

    def unread(name: str) -> tuple[str, list[dict], dict[str, str]]:
        # No read of the field: the document names it where no value was read (a reader
        # limit, why: its first sighting's), else it is silent on it; named only as
        # options of a list ('Filling/Setup'), the mark that picks one is not in the
        # text: unreadable, no reader gap.
        sightings = named.get(name, [])
        seen = [citation for citation, _, _ in sightings][:MAX_SEEN_CITATIONS]
        if seen and all(choice for _, choice, _ in sightings):
            return "unreadable", seen, {}
        why = next((why for _, choice, why in sightings if not choice), None)
        if why == "no_key" and (box := handwritten(name)):   # cited where it is written
            why, seen = HANDWRITTEN_UNKEYED, seen + box
        return (LABEL_SEEN_UNREAD, seen, {"unread": why}) if seen else (unstated, [], {})

    for spec in profile["fields"]:
        name = spec["name"]
        if "keyed" in spec:
            reads = raws.get(name, [])
            entries, unkeyed = _entries(spec, [r for r in reads if r[0]], convention, declared,
                                        read_by)
            citations = [c for _, c, _ in reads]
            reason = ("value_missing" if not entries and not unkeyed else "unreadable"
                      if not entries else None)
            why: dict[str, str] = {}
            if not reads:
                reason, citations, why = unread(name)
            item = {"kind": spec["kind"], "keyed": spec["keyed"], "entries": entries,
                    **why, "citations": citations}
            if citations:
                item["read_by"] = read_by(citations)
            if unkeyed and entries:
                result["gaps"].append({"field": name, "reason": "key_missing",
                                       "citations": unkeyed})
            for entry in entries:
                if entry.get("gap"):
                    result["gaps"].append({"field": name, "key": entry["key"],
                                           "reason": entry["gap"],
                                           "citations": entry["citations"]})
            if reason:
                item["gap"] = reason
                result["gaps"].append({"field": name, "reason": reason, **why,
                                       "citations": citations})
            result["fields"][name] = item
            continue
        reads = _stated_reads(seen.get(name, []))
        citations = [c for _, _, c in reads]
        value, reason, conflict = _settle(reads, readings.get(name, ()))
        why = {}
        if reason == "not_stated":
            reason, citations, why = unread(name)
        label = {"read_by": read_by(citations)} if citations else {}
        if value is not None:
            result["fields"][name] = {"kind": spec["kind"], **value, **label,
                                      "citations": citations}
            continue
        if conflict:   # the declarations it conflicts with are cited too
            codes = {r["currency"] for r in conflict if r["basis"] == "declared"}
            citations = citations + [c for _, raw, c in declarations
                                     if raw.strip().upper() in codes]
        extra = {"readings": conflict} if conflict else why
        result["fields"][name] = {"kind": spec["kind"], "value": None, "gap": reason,
                                  **extra, **label, "citations": citations}
        result["gaps"].append({"field": name, "reason": reason, **extra,
                               "citations": citations})
    if "line_items" in profile:
        result["line_items"], item_gaps = _line_items(profile["line_items"], table_rows,
                                                      convention, declared, read_by)
        result["gaps"].extend(item_gaps)
    split = [(c["page"], c["start"], c["end"]) for field in result["fields"].values()
             if field.get("gap") == SPLIT_AT_GAP for c in field["citations"]]
    for page, page_text in pages:
        for mention in mentions.extract(page_text, quoted=False):
            if mention["normalized"]:
                reference = {
                    "identifier_kind": mention["identifier_kind"], "text": mention["text"],
                    "normalized": mention["normalized"],
                    "label_kind_conflict": mention["label_kind_conflict"],
                    "secondary_of": mention.get("secondary_of"),
                    "citation": cite(page, page_text, mention["start"], mention["end"])}
                if any(at == page and start <= mention["start"] < end
                       for at, start, end in split):   # the read prefix of a split value
                    reference[SPLIT_AT_GAP] = True
                result["references"].append(reference)
    for spec in profile["fields"]:
        if "refers_to" in spec:
            _field_reference(spec, result)
        elif "refers_to_property" in spec:
            _property_reference(spec, result)
    return result


def _field_reference(spec: Mapping[str, Any], result: dict[str, Any]) -> None:
    """The reference a ``refers_to`` field's stated value makes (module docstring,
    *References*): the identifier grammar's reading inside the value's span, marked with
    the field, else the value as the grammar reads it after one of the field's labels
    (:func:`_labelled_reading`), cited by the field's span; nothing when the grammar reads
    no value of that kind there ('PO: 12' is too short to be a PO number)."""
    field = result["fields"].get(spec["name"]) or {}
    kind, value = spec["refers_to"], field.get("value")
    if value is None:
        return
    spans = [(c["page"], c["start"], c["end"]) for c in field["citations"]]
    found = [ref for ref in result["references"] if ref["identifier_kind"] == kind and any(
        page == ref["citation"]["page"] and start <= ref["citation"]["start"] and
        ref["citation"]["end"] <= end for page, start, end in spans)]
    for ref in found:
        ref["field"] = spec["name"]
    normalized = None if found else _labelled_reading(kind, spec["labels"], value)
    if normalized:
        result["references"].append({
            "identifier_kind": kind, "text": value, "normalized": normalized,
            "label_kind_conflict": False, "secondary_of": None, "field": spec["name"],
            "citation": field["citations"][0]})


def _property_reference(spec: Mapping[str, Any], result: dict[str, Any]) -> None:
    """The reference a ``refers_to_property`` field's stated value makes (module docstring,
    *References*): the value as its kind's normaliser reads it, naming the one property it
    resolves on, cited by the field's span; every other reading inside that span is
    dropped, as the span is that value; nothing when the normaliser reads none."""
    field = result["fields"].get(spec["name"]) or {}
    target, value = spec["refers_to_property"], field.get("value")
    if value is None:
        return
    spans = [(c["page"], c["start"], c["end"]) for c in field["citations"]]
    result["references"] = [ref for ref in result["references"] if not any(
        page == ref["citation"]["page"] and start < ref["citation"]["end"] and
        ref["citation"]["start"] < end for page, start, end in spans)]
    try:
        normalized = identifiers.normalize(target["identifier_kind"], value)
    except (KeyError, ValueError):
        normalized = None
    if isinstance(normalized, str) and normalized:
        result["references"].append({
            "identifier_kind": target["identifier_kind"], "text": value,
            "normalized": normalized, "label_kind_conflict": False, "secondary_of": None,
            "field": spec["name"], "property": f"{target['object_type']}.{target['property']}",
            "citation": field["citations"][0]})


def _labelled_reading(kind: str, labels: Iterable[str], value: str) -> str | None:
    """``value`` normalised as the identifier grammar (``ontology.mentions``) reads it
    written after the first of ``labels`` it reads it after as ``kind`` (from the value's
    start, never as another label's kind), else None: the grammar's rule decides."""
    for label in labels:
        text = f"{label}: {value}"
        read = [m for m in mentions.extract(text, kinds=[kind], quoted=False)
                if m["normalized"] and not m["label_kind_conflict"]
                and m["start"] == len(label) + 2]
        if read:
            return read[0]["normalized"]
    return None


def own_record_keys(object_type: str, evidence: Mapping[str, Any]
                    ) -> list[tuple[str, str, str]]:
    """(identifier kind, normalised value, record type) an own record holding this
    document's number would carry: the ``own_record`` of its class's
    ``not_issued_by_own_party`` (module docstring, *Direction*), its field's stated value
    read as the identifier grammar reads it (:func:`_labelled_reading`); empty when the
    class declares none or the field is not stated. ``evidence``: the document's
    extraction as the reader may read it."""
    profile = next((p for p in profiles()["profiles"] if p["object_type"] == object_type
                    and "own_record" in (p.get("not_issued_by_own_party") or {})), None)
    if profile is None:
        return []
    own = profile["not_issued_by_own_party"]["own_record"]
    value = ((evidence.get("fields") or {}).get(own["field"]) or {}).get("value")
    spec = next(f for f in profile["fields"] if f["name"] == own["field"])
    kind = own["identifier_kind"]
    normalized = _labelled_reading(kind, spec["labels"], value) \
        if isinstance(value, str) else None
    return [(kind, normalized, identifiers.kind_spec(kind)["object_type"])] \
        if normalized else []


def resolve_citation(content: bytes, citation: Mapping[str, Any], *, mime_type: str,
                     filename: str = "", local_pdf_ocr: bool = False,
                     text: str | None = None) -> str | None:
    """The literal a field citation names in ``content``, or None when the bytes are not
    the cited ones (SHA-256), the page or span does not exist, or the text there differs.
    A citation of OCR text (``text_sha256``) is checked against ``text``, which must be
    that text (its SHA-256) of these bytes."""
    if sha256(content).hexdigest() != citation.get("source_sha256"):
        return None
    if citation.get("text_sha256") is not None:
        if text is None or sha256(text.encode("utf-8")).hexdigest() != citation["text_sha256"]:
            return None
    elif text is not None:
        text = None   # a text-layer citation is read from the bytes alone
    pages = read_text(content, mime_type, filename=filename, text=text,
                      local_pdf_ocr=local_pdf_ocr).pages
    page = citation.get("page")
    found = next((t for p, t in pages if p == page), None)
    start, end = citation.get("start"), citation.get("end")
    if found is None or type(start) is not int or type(end) is not int or \
            not 0 <= start < end <= len(found) or found[start:end] != citation.get("text"):
        return None
    return found[start:end]


# --------------------------------------------------------------------------- projection

MAX_FIELDS = 1024
_FIELDS: ReadingMemo[dict[str, Any] | None] = ReadingMemo(MAX_FIELDS, lambda _found: 1)


def _fields_of(key: tuple[str, str, str, str | None, str | None],
               read: Callable[[], CapturedText | None]) -> dict[str, Any] | None:
    """The fields of one reading of a document (``read``: ``captured_text`` of a
    captured version's pinned bytes or the capture's OCR text of a scan, or a container
    member's reading), parsed once per ``key`` (bytes, MIME type, name, OCR text, the
    file's provenance signal) and shared by every binding that reads the relation in a
    build (callers copy before changing it); None when the bytes (or the pinned OCR
    text) are missing or unreadable as a document. Neither missing bytes nor a read or
    parse that raised is remembered (a later capture may hold them, a later read may
    succeed; ``captured_text.ReadingMemo``)."""
    def parse() -> dict[str, Any] | None:
        reading = read()
        if reading is None:
            raise NotRemembered(key[0])
        return _fields(reading, None, key[4])
    return _FIELDS.read(key, parse)


def _documents(row: Mapping[str, Any], blobs: str | Path, *, read: bool = False,
               received: Received | None = None
               ) -> Iterator[tuple[Member | None, dict[str, Any] | None,
                               CapturedText | None, str | None]]:
    """The documents of one captured file (``row``: its bytes' digest, type, name, the
    capture's text status and the file's provenance, :func:`_issued_by` with
    ``received``): the file itself (member None), then each member of a container (zip,
    .msg, .eml) on its own (a member has its file's provenance, except that a mail
    message's members are never its sender's own send), each with its fields
    (:func:`_fields_of`), with ``read`` its reading (else None; the file is then read
    only when its fields are not known yet), and its provenance signal; nothing when the
    row names no bytes."""
    digest = row.get("content_sha256")
    mime = row.get("content_type") or row.get("mime_type") or "application/octet-stream"
    name = row.get("name") if isinstance(row.get("name"), str) else ""
    if not isinstance(digest, str) or not isinstance(mime, str):
        return
    issued = _issued_by(row, received)
    reading = captured_text(blobs, row) if read else None
    yield None, _fields_of((digest, mime, name, ocr_digest(row), issued),
                           (lambda: reading) if read else
                           (lambda: captured_text(blobs, row))), reading, issued
    members = container_members(blobs, row)
    for member in () if members is None else members.members:
        # A message forwarded as an attachment is the inner message's: its sender is not
        # the mailbox that forwarded it.
        forwarded = _kind(mime, name) == "message" or any(
            _kind("", part) == "message" for part in member.path.split("!/")[:-1])
        inner = None if issued == "own_mailbox_send" and forwarded else issued
        yield member, _fields_of((member.sha256, member.mime_type, member.name, None, inner),
                                 lambda m=member: m.reading), \
            member.reading if read else None, inner


def captured(records: Iterable[Any], blobs: str | Path) -> Iterator[Any]:
    """``documentFields`` records of captured documents (module docstring): ``records``
    are a capture source's attachment or drive-item records (``CapturedRecord``),
    ``blobs`` its content-addressed byte store. Bytes are re-hashed before reading;
    a document whose bytes are missing, unreadable or of no single class yields
    nothing. One reading per distinct (bytes, MIME type, name, OCR text), shared with
    the mention documents and resolve (``integrations.captured_text``)."""
    from ontology.project import CapturedRecord
    from ontology.store import canonical

    records = list(records)   # a forward is told from its received copy (*Direction*)
    received = _received(rec.row for rec in records)
    for rec in records:
        row = rec.row
        for member, found, _, _ in _documents(row, blobs, received=received):
            if found is None or found["status"] != "classified":
                continue
            handle = byte_pinned(rec.l1_handle, row["content_sha256"])
            source_key, cited, cited_digest = (
                (rec.record_key, handle, row["content_sha256"]) if member is None else
                (canonical([rec.record_key, member.path]),
                 member_handle(handle, member.path, member.sha256), member.sha256))
            out = _record_row(source_key, found, cited, cited_digest)
            yield CapturedRecord(
                source=rec.source, relation=RELATION,
                record_key=canonical([source_key, found["profile"]]), row=out,
                l1_handle=cited, field_prefix="row",
                digest=sha256(canonical(out).encode("utf-8")).hexdigest(),
                recorded_at=rec.recorded_at, superseded_at=rec.superseded_at,
                last_seen_at=rec.last_seen_at, audience_key=rec.audience_key,
                missing_from_source_since=rec.missing_from_source_since,
                removals=rec.removals)


# The kind of a file whose class was never read (``stages``): its type, else its suffix.
_KINDS = (("pdf", ("application/pdf",), (".pdf",)),
          ("image", ("image/",), (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp",
                                  ".webp", ".heic")),
          ("message", ("message/", "application/vnd.ms-outlook"), (".msg", ".eml")),
          ("spreadsheet", ("application/vnd.ms-excel", "application/vnd.openxmlformats-"
                           "officedocument.spreadsheetml", "text/csv"),
           (".xls", ".xlsx", ".xlsm", ".csv", ".ods")),
          ("word", ("application/msword", "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml", "application/rtf"), (".doc", ".docx", ".rtf", ".odt")),
          ("archive", ("application/zip", "application/x-zip"), (".zip",)),
          ("text", ("text/",), (".txt",)))


def _kind(mime: str, name: str) -> str:
    mime, suffix = mime.split(";", 1)[0].strip().lower(), Path(name).suffix.lower()
    return next((kind for kind, types, suffixes in _KINDS
                 if mime.startswith(types) or suffix in suffixes), "other")


@functools.lru_cache(maxsize=1)
def _phrase_patterns() -> tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...]:
    # A class's title phrase anywhere in a line, and the same phrase as a whole line.
    return tuple((p["id"], re.compile(r"(?<!\w)(?:" + _alternation(p["titles"]) + r")(?!\w)",
                                      re.IGNORECASE),
                  re.compile(r"(?:" + _alternation(p["titles"]) + r")", re.IGNORECASE))
                 for p in profiles()["profiles"])


def _signals(read: CapturedText, issued: str | None = None) -> dict[str, set[str]]:
    """Per class, why the text's title phrases of it did not make it this document's class:
    ``mention_beside_title``, ``after_other_title``, ``beside_other_title``,
    ``numbered_title_only``, ``beside_numbered_title``, ``own_issued`` and the provenance
    signals ``issued_category`` / ``own_mailbox_send`` (``issued``, :func:`_classes`),
    ``not_alone`` (a whole-line class's phrase starts a line with more text after it:
    "Purchase Order No: 4500") and ``after_text`` (the phrase ends a line after other text: a letterhead and a
    title on one text line). A phrase inside a sentence is no signal."""
    pages = list(read.pages)
    _, dismissed = _classes(pages, lambda *_: None, issued)
    out = {profile: {reason} for profile, reason in dismissed.items()}
    alone = {p["id"] for p in profiles()["profiles"] if p.get("title_alone")}
    for profile_id, phrase, whole in _phrase_patterns():
        for _, page_text in pages:
            for m in phrase.finditer(page_text):
                start = page_text.rfind("\n", 0, m.start()) + 1
                line = page_text[start:].split("\n", 1)[0]
                before = page_text[start:m.start()].strip()
                after = line[m.end() - start:].strip()
                if whole.fullmatch(line.strip()) or (before and after):
                    continue
                if not before and profile_id in alone:
                    out.setdefault(profile_id, set()).add("not_alone")
                elif before:
                    out.setdefault(profile_id, set()).add("after_text")
    return out


def _prose_titled(read: CapturedText, issued: str | None) -> bool:
    """Whether a class of the document counts only by a prose title (*Class*)."""
    classes, _ = _classes(list(read.pages), lambda page, text, start, end: _PROSE.match(
        text[end:].split("\n", 1)[0]) is not None, issued)
    return any(all(prose) for prose in classes.values())


def stages(rows: Iterable[Mapping[str, Any]], blobs: str | Path) -> dict[str, Any]:
    """Where the reading of each captured document stopped, in counts only (the live
    document-class funnel, ``scripts/eval_document_fields_live.py``). ``rows``: captured
    files (``name``, the MIME type and the capture's ``content_status``,
    ``content_sha256``, ``text_status``, ``text_sha256``), each read as :func:`captured`
    reads it (each read once), the members of a container as documents of their own.
    Each document is counted once, in ``classes`` (by class) or in ``excluded`` by reason:
    ``content_<status>`` (bytes not captured), ``ocr_unavailable`` (pages without a text
    layer this host could not OCR), the capture's text status when it read no text
    (``ocr_failed``, ``ocr_no_text``, ``no_text``, ``unsupported``, ``failed``; a typed
    ``ocr_failed:<cause>`` is ``ocr_failed``, its cause counted in ``ocr_failed_by``,
    ``captured_text.ocr_failure_cause``),
    ``reread_<status>`` (the capture read text, the projection's re-read of the bytes
    none), ``unreadable`` (bytes missing or over the extractor's bounds), ``unclassified``,
    ``ambiguous_class`` or ``own_issued`` (the own party issued it: its text or the file's
    provenance says so, the reason is in ``signals``). ``text``: documents by text status (a member by its
    reading's); ``signals``: per class and reason (:func:`_signals`, plus
    ``capture_text_only``: the capture's own text of the file reads as that class, the
    re-read does not), the documents not of that class whose text holds its title phrase;
    ``ambiguous``: the ``ambiguous_class`` documents by cause, ``prose_title`` (a class
    kept only by a prose title, *Class*) or ``title_lines`` (two classes' title lines);
    ``unread_kinds``: the kind of each document excluded before its class was read."""
    out: dict[str, Any] = {"files": 0, "members": 0, "classes": {}, "excluded": {},
                           "text": {}, "signals": {},
                           "ambiguous": {"prose_title": 0, "title_lines": 0},
                           "unread_kinds": {}, "ocr_failed_by": {}}

    def count(bucket: dict[str, Any], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    rows = list(rows)
    received = _received(rows)
    for row in rows:
        out["files"] += 1
        status = str(row.get("text_status"))
        cause = ocr_failure_cause(status)
        if cause is not None:
            count(out["ocr_failed_by"], cause)
        status = text_status_kind(status)
        mime = str(row.get("content_type") or row.get("mime_type") or "")
        name = row.get("name") if isinstance(row.get("name"), str) else ""
        documents = list(_documents(row, blobs, read=True, received=received))
        if not documents:
            count(out["text"], status)
            count(out["excluded"], "content_" + str(row.get("content_status")).split(":")[0])
            count(out["unread_kinds"], _kind(mime, name))
            continue
        for member, found, reading, issued in documents:
            out["members"] += member is not None
            text = status if member is None else member.text_status
            count(out["text"], text)
            if found is not None and found["status"] == "classified":
                count(out["classes"], found["profile"])
            if found is None or found["status"] not in ("unclassified", "ambiguous_class",
                                                        OWN_ISSUED, "classified"):
                reason = ("unreadable" if found is None else
                          "ocr_unavailable" if text == OCR_UNAVAILABLE else
                          "reread_" + found["status"] if text in TEXT_READ else text)
                count(out["excluded"], reason)
                count(out["unread_kinds"], _kind(*((mime, name) if member is None else
                                                   (member.mime_type, member.name))))
                continue
            if found["status"] != "classified":
                count(out["excluded"], "ocr_unavailable" if text == OCR_UNAVAILABLE
                      else found["status"])
            if found["status"] == "ambiguous_class" and text != OCR_UNAVAILABLE:
                count(out["ambiguous"], "prose_title" if reading is not None and
                      _prose_titled(reading, issued) else "title_lines")
            signals = _signals(reading, issued) if reading is not None else {}
            if member is None and status in TEXT_READ and status != TEXT_OCR and \
                    found["status"] != "classified":
                kept = read_blob(blobs, row.get("text_sha256"))
                own = None if kept is None else _fields(
                    read_text(b"", "text/plain", text=kept.decode("utf-8")), None, issued)
                if own is not None and own["status"] == "classified" and \
                        own["profile"] != found.get("profile"):
                    signals.setdefault(own["profile"], set()).add("capture_text_only")
            for profile_id, reasons in sorted(signals.items()):
                if profile_id != found.get("profile"):
                    for reason in sorted(reasons):
                        count(out["signals"].setdefault(profile_id, {}), reason)
    return out


DOCUMENT_KEYS = ("l1_handle", "source_sha256", "text_sha256", "text_source")


def full_citation(evidence: Mapping[str, Any], citation: Mapping[str, Any]) -> dict[str, Any]:
    """A citation of a projected ``evidence`` (page, span, literal) with the document-level
    keys it shares with every other one (handle, byte and OCR text SHA-256), as
    :func:`resolve_citation` takes it."""
    return {**{k: evidence[k] for k in DOCUMENT_KEYS if k in evidence}, **citation}


def _compact(value: Any) -> Any:
    """An extraction whose citations leave out the document-level keys (one document,
    one handle and digest: stated once at the top, :func:`full_citation`)."""
    if isinstance(value, dict):
        cited = "page" in value and "start" in value
        return {k: _compact(v) for k, v in value.items() if not (cited and k in DOCUMENT_KEYS)}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _record_row(source_key: str, found: Mapping[str, Any], handle: str, digest: str
                ) -> dict[str, Any]:
    """The ``documentFields`` row of one classified extraction (cited by ``handle``); its
    ``evidence`` states the handle and digests once (:func:`full_citation`)."""
    evidence = _compact(_with_handle(found, handle))
    fields = {name: {k: (v if k not in ("entries", "alternates") else [
        {ek: ev for ek, ev in entry.items() if ek != "citations"} for entry in v])
        for k, v in item.items() if k != "citations"}
        for name, item in evidence["fields"].items()}
    return {"source_key": source_key, "profile": found["profile"], "document": handle,
            "content_sha256": digest, "tier": TIER, "fields": fields, "evidence": evidence}


def _pinned(handle: Any) -> tuple[str, str, str] | None:
    """(source, source key, pinned SHA-256) of a whole-document byte-pinned citation: a
    SharePoint file version (``sharepoint://<drive>/<item>@<eTag>#sha256=<hex>``) or a mail
    file attachment (``graph-mail://...#attachment=<id>&sha256=<hex>``); else None (a page,
    a container member, any other fragment key or scheme)."""
    from integrations.capture_sharepoint import SCHEME
    from integrations.graph_evidence import parse_mail_attachment_citation
    from integrations.live_source_guard import parse_handle
    from ontology.store import canonical

    try:
        if str(handle).startswith(SCHEME + "://"):
            (drive_id, item_id), _, extra = parse_handle(handle, SCHEME, 2)
            if set(extra) != {"sha256"} or not re.fullmatch(r"[0-9a-f]{64}", extra["sha256"]):
                return None
            return "sharepoint", canonical([drive_id, item_id]), extra["sha256"]
        cited = parse_mail_attachment_citation(handle)
    except (TypeError, ValueError):
        return None
    return ("graph_mail", canonical([cited["mailbox"], cited["message_id"],
                                     cited["attachment_id"]]), cited["sha256"])


def pinned_document_row(handle: str, l1_handle: str, content: bytes, row: Mapping[str, Any],
                        text: str | None = None) -> dict[str, Any]:
    """The ``documentFields`` row of a byte-pinned document citation (:func:`_pinned`) as
    its bytes state it now: ``content`` the bytes a gate already served the reader
    (refused unless they hash to the handle's pin), ``row`` the capture's row of the
    version (``name``, ``content_type`` or ``mime_type``; its provenance signal,
    :func:`_issued_by`, with a mail attachment's sender provenance ``message_from`` and
    ``mailbox_upn`` and its ``mailbox_copies``, the capture's rows :func:`_received`
    tells a forward from, as the mail projection reads them), ``text`` the capture's
    OCR text of a scan (else None),
    re-extracted; ``l1_handle`` the version's capture handle (the row cites it
    byte-pinned, as the projection does). ``{"ok", "relation", "row",
    "sha256"}``; anything else (another citation, other bytes, no single class):
    ``ok`` false."""
    refused = {"ok": False, "error": "citation_unresolvable"}
    pinned = _pinned(handle)
    if (pinned is None or not isinstance(content, bytes)
            or sha256(content).hexdigest() != pinned[2]):
        return refused
    source, source_key, digest = pinned
    mime = row.get("content_type") or row.get("mime_type") or "application/octet-stream"
    try:
        found = extract_fields(content, str(mime), filename=str(row.get("name") or ""),
                               text=text, issued=_issued_by(
                                   row, _received(row.get("mailbox_copies") or ())))
    except Exception:  # noqa: BLE001 - unreadable now: no evidence
        return refused
    if found["status"] != "classified":
        return refused
    return {"ok": True, "relation": f"{source}.{RELATION}",
            "row": _record_row(source_key, found, byte_pinned(l1_handle, digest), digest),
            "sha256": digest}


def capture_citation_reader(store: Any, actor: Any, audience: Any
                            ) -> Callable[[str], dict[str, Any]]:
    """``handle -> read`` of a byte-pinned SharePoint capture citation of a projected
    document (``sharepoint://...#sha256=<hex>``), as ``actor`` may read it now: the
    capture store's item gate (``SharePointCaptureStore.resolve``), the pinned bytes and
    OCR text re-extracted (:func:`pinned_document_row`), the ``documentFields`` row they
    give. ``sha256`` is the bytes' digest (what a pinned original is matched by). Anything
    else: ``ok`` false."""

    def read(handle: str) -> dict[str, Any]:
        if _pinned(handle) is None:
            return {"ok": False, "error": "citation_unresolvable"}
        view = store.resolve(handle, actor, audience)
        content = None if view is None else store.content(handle, actor, audience)
        if view is None or content is None:
            return {"ok": False, "error": "citation_unresolvable"}
        row = view["row"]
        text = row.get("content_text") if row.get("text_status") == TEXT_OCR else None
        return pinned_document_row(handle, view["l1_handle"], content, row, text)
    return read


def comparisons() -> list[dict[str, Any]]:
    """Every declared comparison (``attests``): the document type, the link type from
    the document to the attested record, and per rule the field, whether it is keyed,
    the path from the attested record and the property, event or key path compared, a
    code field's vocabulary and the owner-system proposal type (``propose``) a value the
    record lacks may be proposed through."""
    out = []
    for profile in profiles()["profiles"]:
        attests = profile.get("attests")
        if not attests:
            continue
        specs = {spec["name"]: spec for spec in profile["fields"]}
        for rule in attests["compare"]:
            out.append({"document_type": profile["object_type"],
                        "link_type": attests["link_type"],
                        "kind": specs[rule["field"]]["kind"],
                        "keyed": "keyed" in specs[rule["field"]], "path": [],
                        "tax_basis": specs[rule["field"]].get("tax_basis", "unknown"),
                        "vocabulary": profiles()["vocabularies"].get(
                            specs[rule["field"]].get("vocabulary", ""), []),
                        **rule})
    return out


def proposed_fields(profile: Any, proposal_type: Any) -> frozenset[str]:
    """The fields of document profile ``profile`` (an id) whose ``attests`` rule names
    ``proposal_type`` (``propose``): the only fields of such a document that may supply
    a value of a proposal of that type."""
    kinds = {item["id"]: item["object_type"] for item in profiles()["profiles"]}
    return frozenset(rule["field"] for rule in comparisons()
                     if rule.get("propose") is not None and rule["propose"] == proposal_type
                     and rule["document_type"] == kinds.get(profile))


def _with_handle(value: Any, handle: str) -> Any:
    """A copy of an extraction result whose citations name ``handle``."""
    if isinstance(value, dict):
        return {k: (handle if k == "l1_handle" else _with_handle(v, handle))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_with_handle(v, handle) for v in value]
    return value


STORE_LINK_BATCH = 500


def store_document_links(builder: Any, projector: Any, *, aliases: Any = None
                         ) -> dict[str, Any]:
    """Write every reference of every projected document-field object as ``lead`` links
    of its type's declared ``mention`` link types, into the generation being built (a
    ``build_store`` ``post_project`` step). Candidates come from the generation's own
    identifier index (``mentions``' lookup: the kind and its alias kinds) and ``aliases``
    (``ontology.reviews.BuildAliases``, the reviewed aliases), limited to the
    time both versions overlap; several candidates stay several links sharing one
    ``mention_group``. Readers are re-gated on read (both ends, every cited handle)."""
    conn = builder.connection
    types = sorted({p["object_type"] for p in profiles()["profiles"]})
    declared = {pair: link for pair, link in
                mentions.mention_link_types(getattr(projector, "links", {}) or {}).items()
                if pair[0] in types}
    counts = {"documents": 0, "references": 0, "not_found": 0, "undeclared_target": 0,
              "dropped_count": 0, "links": 0}
    rows: list[dict[str, Any]] = []
    marks = ",".join("?" * len(types))
    versions = mentions._query(
        conn, "SELECT o.object_id, o.object_type, v.l1_handle, v.digest, v.lineage, "
        f"v.markings, v.recorded_at, v.superseded_at, {mentions.REMOVALS_COLUMN}, "
        "ev.value AS evidence FROM objects o "
        "JOIN object_versions v ON v.object_id = o.object_id JOIN object_values ev ON "
        "ev.object_id = v.object_id AND ev.l1_handle = v.l1_handle AND ev.property = "
        f"'evidence' WHERE o.object_type IN ({marks}) ORDER BY o.object_id, v.recorded_at",
        types).fetchall()
    for source in versions:
        counts["documents"] += 1
        evidence = json.loads(source["evidence"])
        spans_doc = mentions.held_spans(source)
        seen: set[str] = set()
        for ref in evidence.get("references", ()):
            counts["references"] += 1
            mention = {"identifier_kind": ref["identifier_kind"],
                       "normalized": ref["normalized"], "text": ref["text"]}
            candidates = [(row, window, held) for row in
                          mentions._store_candidates(conn, mention, source["object_id"],
                                                     aliases)
                          for held, span in enumerate(spans_doc)
                          if (window := mentions._overlap(
                              span, (row["recorded_at"], row["superseded_at"])))]
            if not candidates:
                counts["not_found"] += 1
                continue
            cite = full_citation(evidence, ref["citation"])
            group = _digest(source["object_id"], cite["page"], cite["start"], cite["end"])
            dropped: set[tuple[str, str]] = set()
            for row, window, held in candidates:
                if ref.get("property") and (row["identifier_kind"] != ref["identifier_kind"]
                                            or f'{row["object_type"]}.{row["property"]}'
                                            != ref["property"]):
                    # Never an alias kind or another holder; counted once per candidate
                    # version, however many held spans of the document it overlaps.
                    dropped.add((row["object_id"], row["l1_handle"]))
                    continue
                link_type = declared.get((source["object_type"], row["object_type"]))
                if link_type is None:
                    counts["undeclared_target"] += 1
                    continue
                link_id = ("docfield:" + _digest(group, row["object_id"], row["l1_handle"])
                           + (f"#{held}" if held else "") + mentions.candidate_piece(row))
                if link_id in seen:
                    continue
                seen.add(link_id)
                rows.append(_link(source, {**ref, "citation": cite}, row, window, link_type,
                                  link_id, group))
                if len(rows) >= STORE_LINK_BATCH:
                    counts["links"] += builder.add("links", rows)
                    rows = []
            counts["dropped_count"] += len(dropped)
    if rows:
        counts["links"] += builder.add("links", rows)
    return {"format": FORMAT, "extractor": extractor_version(), "tier": TIER,
            "counts": counts, "link_tiers": {TIER: counts["links"]}}


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _link(source: Any, ref: Mapping[str, Any], row: Any, window: tuple[str, str | None],
          link_type: str, link_id: str, group: str) -> dict[str, Any]:
    cite = ref["citation"]
    via, cited_handles = mentions.candidate_via(row)
    reason = [
        {"feature": "document_reference", "agrees": True, "mention_group": group,
         "identifier_kind": ref["identifier_kind"], "normalized": ref["normalized"],
         "citations": [{"l1_handle": source["l1_handle"], "field_path": "row.evidence",
                        "digest": source["digest"], "lineage": source["lineage"],
                        "source_sha256": cite["source_sha256"], "page": cite["page"],
                        "span": [cite["start"], cite["end"]]}]},
        {"feature": "identity_candidate", "agrees": True, **via,
         "matched_kind": row["identifier_kind"], "matched_normalized": row["normalized"],
         "property": row["property"], "role": row["role"],
         "citations": [{"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                        "digest": row["digest"], "lineage": row["lineage"]}]}]
    why_not = [{"feature": "machine_read", "agrees": None,
                "detail": "read from document text by the deterministic extractor; a lead "
                          "until a person reviews it"},
               {"feature": "external_assertion", "agrees": False,
                "cannot_confirm": list(mentions.EXTERNAL_UNCONFIRMABLE)}]
    if ref.get("label_kind_conflict"):
        why_not.append({"feature": "label_kind_conflict", "agrees": False,
                        "label_kind": ref.get("secondary_of")})
    if ref.get(SPLIT_AT_GAP):
        why_not.append({"feature": SPLIT_AT_GAP, "agrees": False,
                        "detail": "a column gap follows the read digits; the identifier "
                                  "may continue past it"})
    return {"link_id": link_id, "link_type": link_type, "from_object": source["object_id"],
            "to_object": row["object_id"], "tier": TIER, "binding_kind": mentions.BINDING_KIND,
            "link_contract": None,
            "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
            "why_not": json.dumps(why_not, sort_keys=True, separators=(",", ":")),
            "evidence_handles": json.dumps(list(dict.fromkeys(
                [source["l1_handle"], row["l1_handle"], *cited_handles])),
                separators=(",", ":")),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": mentions._marks(source["markings"], row["markings"]),
            "recorded_at": window[0], "superseded_at": window[1]}


__all__ = ["FORMAT", "PROFILES_PATH", "RELATION", "TIER", "capture_citation_reader",
           "captured", "comparisons", "extract_fields", "extractor_version", "full_citation",
           "pinned_document_row", "profiles", "proposed_fields", "resolve_citation", "stages",
           "store_document_links"]
