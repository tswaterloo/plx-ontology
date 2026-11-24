"""plx.sql's description of what it reads (split from ``ontology_tools_sql``): the
rules the Ask is given, the generic and comms-derived tables, the record-type catalog
lines and the served views. ``ontology_tools_sql`` re-exports every name, so callers
and tests keep reading them there."""
from __future__ import annotations

import json
import time
from typing import Any, Sequence

from operating.pg import business_views as bv
from operating.pg import sql_views


RULES = (
    "One SELECT per call over these views (bare names); a long result comes back cut and says "
    "so. Each row is "
    "one real thing (its Portal, Sage, BC and FileMaker copies joined); records deleted at "
    "the source are left out; money columns sit beside a <column>_currency column (CAD and "
    "USD rows exist). Dates are business dates (America/"
    "New_York). sales_invoices.pilot = true marks Business Central invoices. Text compares "
    "case-sensitively: use ILIKE "
    "'%word%' for names. An array column (addresses, domains, customers, suppliers, "
    "records) is indexed for containment: domains @> ARRAY['univar.com'] (exact, lower "
    "case) uses the index, where unnest() with ILIKE reads every row and runs past the "
    "time limit. A column in a row's withheld list is one you may not read (NULL). "
    "Each row has a handle: select it to cite the record. A result row's cells line up "
    "with its columns.")
# the generic views' rule (``records``), said only while they are served (``served_views``)
RULES_GENERIC = (
    " EVERY other captured record of every system (Mission Control, "
    "SharePoint lists, FileMaker, Portal, BC and Sage tables, Planner, records read out of "
    "mail ...) is in the generic views: record_types (the catalog of types: plain name, "
    "systems, fields with kind and meaning, label and date fields, links out/in), records "
    "(one row per record) and record_links (one row per link between two records). Filter "
    "records on type = '<Type>' and read a field as fields->>'name' (numbers: "
    "(fields->>'name')::numeric; money beside fields->>'name_currency', quantities beside "
    "fields->>'name_unit'). The same thing held by two systems is two records joined by an "
    "identity link (record_types.identity_links, in record_links). {describe: '<Type>'} "
    "shows a type's fields, sample records and commonest values. records.label ILIKE is "
    "indexed, and type = '<Type>' first is fast; fields::text ILIKE over every type "
    "reads every record and runs past the time limit: find a word in any record with "
    "plx.search (sources records). A record's links: record_links WHERE from_id (or "
    "to_id) = ANY(ARRAY(SELECT record_id FROM records WHERE type = '<Type>' AND ...)) "
    "reads them by index. To join views row by row (records to "
    "record_links, a link to the record at its other end, records to a business view), "
    "read the other side in a fenced lateral: LEFT JOIN LATERAL (SELECT l.to_id FROM "
    "record_links l WHERE l.from_id = r.record_id AND l.link_type = '<link>' OFFSET 0) l "
    "ON true (OFFSET 0 is what makes each row's match an index read); a "
    "plain JOIN, IN (SELECT ...) or a lateral without OFFSET 0 between these views reads "
    "every row of the other one (past the time limit). "
    "Mail and Teams: a count or list within a received_at / created_at window reads its "
    "index; over the whole table it reads every message.")
RULES_TAIL = " Mail text, chats and documents: plx.search."
RULES_BASE = RULES
RULES = RULES_BASE + RULES_GENERIC + RULES_TAIL

GENERIC_ABOUT = {
    "record_types": ("the catalog of every record type the store holds (generated from "
                     "the ontology: new sources and fields appear by themselves)", {
                         "type": "text: the type's id (records.type)",
                         "name": "text: its plain name", "description": "text",
                         "systems": "text[]: the systems holding records of it",
                         "sources": "text[]: the source tables (records.source)",
                         "label_fields": "text[]: fields that name a record",
                         "date_fields": "text[]: fields that date a record; the first is its date of record",
                         "dates": "jsonb: [{field, role, means, dated_share, oldest, newest, "
                                  "future_share}]: each declared date, what it means (role "
                                  "record = the date of record, stamp = the system's own "
                                  "last-write day, other) and the share of the type's records "
                                  "that carry it; a type with none has no date of record "
                                  "(records.record_date is NULL)",
                         "identity_links": "text[]: link types joining copies of one thing",
                         "fields": "jsonb: [{name, kind, description}]",
                         "links_out": "jsonb: [{link_type, to}]",
                         "links_in": "jsonb: [{link_type, from}]",
                         "tables": "text[]: the views holding its records (a curated view, "
                                   "a mail/Teams view, or records)"}),
    "records": ("one row per current record of a type no curated view lists, as this "
                "reader may read it (count a type here)", {
        "record_id": "text: the record's id (record_links.from_id / to_id)",
        "type": "text: the record type (filter on it)", "system": "text",
        "source": "text: the source table", "label": "text: its display name or number",
        "record_date": "text YYYY-MM-DD: its date of record (the first of record_types.date_fields); "
                       "where that date's record_types.dates role is stamp it is only the day the "
                       "source system last wrote the row, not a business date: never read it as "
                       "when the thing happened",
        "first_seen": "text: when it was first captured", "changed_at": "text",
        "fields": "jsonb: field -> value (the authority-first value of each field)"}),
    "record_links": ("one row per live link between two records this reader may read "
                     "(filter from_id or to_id = ANY(ARRAY(SELECT record_id FROM records "
                     "WHERE ...)), never a JOIN to records)", {
        "from_id": "text: records.record_id", "from_type": "text", "from_handle": "text",
        "link_type": "text", "to_id": "text: records.record_id", "to_type": "text",
        "to_handle": "text", "tier": "text: verified/reviewed/corroborated (solid), "
        "stored_by_text_join, lead (a guess)"}),
}
GENERIC = tuple(GENERIC_ABOUT)
# views over the communications views (``operating.pg.comms_findings``): a statement naming
# one reads with the communications settings (its rows are those of messages the reader's
# own mail and Teams views show; unset, it shows nothing)
COMMS_DERIVED_ABOUT = {
    "comms_findings": (
        "decisions, requests, complaints and praise read out of mail and Teams messages "
        "(one row per item a message states; a message this reader cannot read shows none; "
        "the summary is a model's one-line reading: open the handle with plx.open to quote "
        "the message)", {
            "kind": "text: decision, request, complaint or praise",
            "topic": "text: delivery, pricing, quality, documents, order, payment, product, "
                     "samples, packaging, schedule, stock, service or other",
            "summary": "text: one plain sentence saying what was decided, asked or "
                       "reported",
            "who": "text: who decided, asked or complained, as the message names them "
                   "('' not named)",
            "from_side": "text: who sent the message: us (our staff), customer, outside "
                         "(another party: supplier, carrier, vendor) or '' (unknown: a Teams "
                         "sender not matched to staff)",
            "said_at": "timestamptz: when the message was sent",
            "said_date": "date: its business date",
            "source": "text: email or teams",
            "sender": "text: the message's sender (an address for mail, a name in Teams)",
            "subject": "text: the mail subject or Teams chat topic",
            "thread": "text: the mail thread (emails.thread) or Teams chat id",
            "customers": "text[]: handles of customers rows the message is about (join "
                         "customers ON handle = ANY(customers))",
            "suppliers": "text[]: handles of suppliers rows it is about"}),
    "teams_message_files": (
        "the file a Teams message shares, linked to the captured SharePoint file it is (one "
        "row per message and file; exact address match, no name guessing: a shared file not "
        "captured has no row). A row shows only when this reader may read the message AND "
        "the file (files: full readers only). Join teams_messages ON handle = message_handle "
        "and sharepoint_files ON handle = file_handle; open file_handle with plx.open or "
        "read it with plx.read_document", {
            "message_handle": "text: the Teams message's handle (teams_messages.handle)",
            "file_handle": "text: the file's handle (sharepoint_files.handle)",
            "file_name": "text: the file's name", "file_path": "text: the file's path",
            "mime_type": "text: the file's type", "size": "numeric: bytes",
            "url": "text: the address the message names (the file's SharePoint address)",
            "via": "text: url_exact (the message's address is the file's) or url_owner_path "
                   "(the file in that person's OneDrive at that path)"}),
}
COMMS_DERIVED = tuple(COMMS_DERIVED_ABOUT)
GENERIC_TABLES = ("records", "record_links")
TYPE_SAMPLE = 500


_CATALOG: dict[str, Any] = {}


# the fields naming what kind of record a row is ("PaymentNotice", "received", "Open"):
# named in its catalog line so the Ask filters the type by them instead of searching text
CATALOG_KEYS = ("record_type", "family", "kind", "status", "state", "stage", "category",
                "document_type")


def catalog_keys(fields: Any) -> list[str]:
    """The ``CATALOG_KEYS`` a type's ``record_types.fields`` holds, in that order, at most 3
    (the catalog's own field list: no row of any record)."""
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except ValueError:
            return []
    names = {f.get("name") for f in fields or () if isinstance(f, dict)}
    return [k for k in CATALOG_KEYS if k in names][:3]


def date_bit(dates: Sequence[str], stamp: Any) -> str:
    """The prompt's words for a type's dates: the first two, each one that is only the
    source system's last-write day (``role`` stamp in ``record_types.dates``) said to be."""
    if not dates:
        return ""
    shown = list(dates[:2])
    marked = [f for f in shown if f in stamp]
    return f"date {'/'.join(shown)}" + (
        f" ({', '.join(marked)}: last-write stamp, not a business date)" if marked else "")


def catalog_lines(pg: Any = None) -> list[str]:
    """The record types the door holds as prompt lines, one per system: each type with its
    plain name, the fields that label and date it and, when a curated or communications
    view holds it instead of ``records``, that view; generated from the served
    catalog (``<biz>.record_types``, read by the store's role: no row of any record, no
    count). Kept per process until the catalog is built again; none when it cannot be read
    (the prompt then lists the views alone)."""
    try:
        if pg is None:
            from operating.pg import pool
            with pool.connect(connect_timeout=3, options="-c statement_timeout=2000") as c:
                return catalog_lines(c)
        if pg.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.record_types",)
                      ).fetchone()[0] is None:
            return []
        # the catalog's build and where each type is held (``generic_views.mark_tables``
        # moves ``tables`` without a new catalog build)
        (built,) = pg.execute(f"SELECT max(built_at) || md5(string_agg(type || ':' || "
                              "array_to_string(tables, ','), ';' ORDER BY type)) FROM "
                              f"{bv.SCHEMA}.record_types").fetchone()
        if _CATALOG.get("built") == built and built is not None:
            return list(_CATALOG["lines"])
        from operating.pg import generic_views
        rows = pg.execute(
            f"SELECT t.type, t.name, t.systems, t.label_fields, t.date_fields, t.tables, "
            f"t.fields, t.dates FROM "
            f"{bv.SCHEMA}.record_types t WHERE cardinality(t.tables) > 0 AND "
            f"{generic_views.listed_sql('t')} ORDER BY t.type").fetchall()
    except Exception:  # noqa: BLE001 - no catalog: the views alone
        return []
    by_system: dict[str, list[str]] = {}
    for row in rows:
        tid, name, systems, labels, dates, tables = row[:6]
        fields = row[6] if len(row) > 6 else None
        # the date of record says what it is when it is only the system's last-write stamp
        stamp = {d.get("field") for d in (row[7] if len(row) > 7 and row[7] else [])
                 if d.get("role") == "stamp"}
        # a type held by a curated or comms view is named with it (records lacks it)
        bits = [f"label {'/'.join(labels[:2])}" if labels else "",
                date_bit(dates, stamp),
                "in " + "/".join(tables) if list(tables) != ["records"] else "",
                # the fields that say what kind of record a row is (field names only)
                "kinds by " + "/".join(keys) if (keys := catalog_keys(fields)) else ""]
        shown = f"{tid} ({name}" + "".join(f"; {b}" for b in bits if b) + ")"
        for system in (systems or ["unknown"]):
            by_system.setdefault(str(system), []).append(shown)
    lines = [f"- {system}: " + ", ".join(sorted(types))
             for system, types in sorted(by_system.items())]
    _CATALOG.update(built=built, lines=lines)
    return list(lines)


_SERVED: dict[str, Any] = {}
SERVED_RECHECK_SECONDS = 60.0


def served_views(names: Sequence[str] | None = None, pg: Any = None) -> frozenset[str] | None:
    """Which of the views ``names`` (default: every view of ``_dictionary``) the served
    schema holds now (a business table built, the mail and Teams tables once their build
    is served, ``records`` once the generic build is): what the Ask's prompt lists, so the
    model is never sent to a table it would be refused (11-18: emails/teams_messages and
    records listed while not built, every mail question spent its calls on
    'unknown view'). Read by the store's role (no row of any table), kept per process
    ``SERVED_RECHECK_SECONDS``; None when it cannot be read (the prompt lists them all)."""
    wanted = tuple(names if names is not None else _dictionary())
    held = _SERVED.get(wanted)
    if held is not None and time.monotonic() - held[0] < SERVED_RECHECK_SECONDS:
        return held[1]
    try:
        if pg is None:
            from operating.pg import pool
            with pool.connect(connect_timeout=3, options="-c statement_timeout=2000") as c:
                return served_views(wanted, c)
        rows = pg.execute("SELECT name FROM unnest(%s::text[]) name WHERE "
                          "to_regclass(%s || '.' || name) IS NOT NULL",
                          (list(wanted), bv.SCHEMA)).fetchall()
    except Exception:  # noqa: BLE001 - not known: every view listed, as before
        return None
    got = frozenset(r[0] for r in rows)
    _SERVED[wanted] = (time.monotonic(), got)
    return got


def view_names() -> tuple[str, ...]:
    """Every view the model is told it may query (the names of ``_dictionary``)."""
    return tuple(_dictionary())


# The view docs as the train-B docs change writes them (what each column holds, no
# system-of-record or pilot verdict), served to the model until train B carries that branch:
# its SYSTEMS_DOC edit moves every view's fingerprint, so only a Spark train may change the view
# code. (view, column) -> doc; column "" is the view's about. HOLD (guardrail 5): delete this
# mapping in the first landing after train B's swap; owner ASK, logged in DEFERRED-1122.
DOC_UNTIL_TRAIN: dict[tuple[str, str], str] = {
    ('batches', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('customer_balances', ''):
        'What customers owe now: every open (unpaid) Sage AR document with its remaining '
        "balance, aged by due date, in its own currency. Business Central's aged receivab"
        'les are not in this view.',
    ('customer_balances', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('customers', 'currency'):
        "text: the customer's billing currency: its Sage account's (Customer.currency of "
        'the Sage copy, the account its invoices and balances are booked on), else the fi'
        "rst other copy's",
    ('customers', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('products', 'category'):
        "text: item category: the item's category, read from Sage's item master first (it"
        's codes are product lines such as PLABEL, LIQUID, VCARE; no Sage category table '
        "is captured, so the code stands), else from BC's item category (PACK, FG, DIST, "
        'RAW: a different, coarser list)',
    ('products', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('purchase_order_lines', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('purchase_orders', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('receivable_documents', ''):
        'Every Sage AR document ever booked, paid or open: invoice, credit note, receipt '
        'and the rest, with its date, due date and the date Sage shows it paid (paid_date'
        '). For the invoices owed on a past day D: sum original_amount of the invoices wi'
        "th document_date <= D and (status = 'open' or paid_date > D), per currency. Appr"
        'oximate: Sage dates only the final payment (paid_date), so a part payment before'
        ' D is not netted, and receipts and prepayments (live 11-22: 25,407 and 1,526 of '
        'them) and 416 invoices Sage marks paid carry no paid date, so restrict history t'
        "o document_type = 'invoice' and say so. What is owed today is customer_balances."
        " Business Central's aged receivables are not in this view.",
    ('receivable_documents', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('sales_invoice_lines', ''):
        'Every line of every customer invoice (sales_invoices: Sage first; Business Centr'
        'al lines carry pilot = true): product, quantity and its unit, revenue (before ta'
        'x, after the invoice discount), cost and margin per line, each in its own curren'
        'cy. Sage states unit cost in the home currency (CAD): a line sold in another cur'
        "rency has its cost converted at its own invoice's exchange rate for the margin ("
        'margin_basis = cost_converted_at_invoice_rate; cost stays as stated, in CAD).',
    ('sales_invoice_lines', 'category'):
        "text: the product's category: the item's category, read from Sage's item master "
        'first (its codes are product lines such as PLABEL, LIQUID, VCARE; no Sage catego'
        "ry table is captured, so the code stands), else from BC's item category (PACK, F"
        'G, DIST, RAW: a different, coarser list)',
    ('sales_invoice_lines', 'pilot'):
        'boolean: true: a Business Central record (its rows are in this view; filter on p'
        'ilot)',
    ('sales_invoice_lines', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('sales_invoices', ''):
        'Every customer invoice once, Sage first; Business Central invoices carry pilot ='
        " true. Each amount in the invoice's own currency; total includes tax, subtotal i"
        's before tax.',
    ('sales_invoices', 'pilot'):
        'boolean: true: a Business Central record (its rows are in this view; filter on p'
        'ilot)',
    ('sales_invoices', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('sales_order_lines', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('sales_orders', 'of_record'):
        'boolean: true: a Sage order header carries the order; false: held only in FileMa'
        'ker order entry, the Portal or BC',
    ('sales_orders', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('sales_orders', 'status_basis'):
        "text: sage: Sage's own order-complete code (1/2 open, 3/4/5 complete) for a Sage"
        ' order; rule: the cross-system sales-order rule (Portal, FileMaker and BC copies'
        ')',
    ('staff', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('stock', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('supplier_balances', ''):
        'What PLX owes suppliers now: every open (unpaid) Sage AP document with its remai'
        'ning balance, aged by due date, in its own currency (prepayments and credit note'
        "s negative). Business Central's aged payables are not in this view.",
    ('supplier_balances', 'document_type'):
        "text: the document's kind by Sage's own type (txttrxtype): invoice, debit note, "
        'credit note, interest, unapplied cash, prepayment, payment; other: a type Sage n'
        'ames that is none of these. A count or total of invoices (bills) is document_typ'
        "e = 'invoice'; payments, prepayments, unapplied cash and credit notes are not in"
        'voices; a Business Central purchase invoice is an invoice',
    ('supplier_balances', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('supplier_invoices', ''):
        'Every supplier document once: Sage AP documents of every kind (invoices, debit a'
        'nd credit notes, interest, prepayments, payments; paid or not: document_type tel'
        "ls them apart, an invoice count is document_type = 'invoice') and Business Centr"
        "al's purchase invoices (pilot = true). Amounts in the document's own currency.",
    ('supplier_invoices', 'document_type'):
        "text: the document's kind by Sage's own type (txttrxtype): invoice, debit note, "
        'credit note, interest, unapplied cash, prepayment, payment; other: a type Sage n'
        'ames that is none of these. A count or total of invoices (bills) is document_typ'
        "e = 'invoice'; payments, prepayments, unapplied cash and credit notes are not in"
        'voices; a Business Central purchase invoice is an invoice',
    ('supplier_invoices', 'payee_class'):
        "text: who the payable is owed to, by the GL account Sage posts the vendor's invo"
        "ices to (the vendor's default account's group category): trade (inventory and co"
        'st of sales: raw materials, packaging, freight in, product testing), expense (se'
        'rvices, freight out, rent, IT, consulting), capital (fixed assets), tax_payroll '
        '(source deductions, sales tax, workplace insurance, income tax), finance (cards,'
        ' lenders), other, or unknown (no default account in Sage, e.g. card issuers). Tr'
        "ade payables = payee_class 'trade'; a Business Central invoice: unknown",
    ('supplier_invoices', 'pilot'):
        'boolean: true: a Business Central record (its rows are in this view; filter on p'
        'ilot)',
    ('supplier_invoices', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
    ('supplier_balances', ''):
        'What PLX owes suppliers now: every open (unpaid) Sage AP document with its remain'
        'ing balance, aged by due date, in its own currency (prepayments and credit notes'
        " negative). The rows are Sage's (source_systems sage); Business Central's purcha"
        'se invoices are in supplier_invoices (source_systems bc).',
    ('supplier_invoices', ''):
        'Every supplier document once: Sage AP documents of every kind (invoices, debit an'
        "d credit notes, interest, prepayments, payments; paid or not: document_type tell"
        "s them apart, an invoice count is document_type = 'invoice') and Business Centra"
        "l's purchase invoices (source_systems bc, pilot = true). Amounts in the document"
        "'s own currency.",
    ('suppliers', 'source_systems'):
        'text: the systems holding a copy of this row, sorted and comma separated: bc (Bu'
        "siness Central), filemaker, portal (the Portal app), sage; e.g. 'bc,filemaker,po"
        "rtal' (one system's rows: source_systems LIKE '%bc%'); the row's other columns a"
        "re the joined thing's. 'bc,portal' is held in Business Central and the Portal",
}


def _dictionary() -> dict[str, Any]:
    out = {}
    for name, entry in bv.dictionary().items():
        cols = {c: DOC_UNTIL_TRAIN.get((name, c), doc) for c, doc in entry["columns"].items()}
        entry = {**entry, "about": DOC_UNTIL_TRAIN.get((name, ""), entry["about"])}
        for cname, kind, cdoc in sql_views.SHOWN_COMMON:
            cols[cname] = f"{kind}: {cdoc}"
        out[name] = {"about": entry["about"], "columns": cols}
    for name, (about, cols) in {**GENERIC_ABOUT, **COMMS_DERIVED_ABOUT}.items():
        cols = dict(cols)
        if name != "record_types":
            for cname, kind, cdoc in sql_views.SHOWN_COMMON:
                cols[cname] = f"{kind}: {cdoc}"
        out[name] = {"about": about, "columns": cols}
    return out
