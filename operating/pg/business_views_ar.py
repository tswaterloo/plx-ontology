"""The receivable documents view: every Sage AR document, paid or open, with the date Sage
paid it, so the receivables of a past day can be read (``customer_balances`` holds only
what is open now). Registered by ``operating.pg.business_views`` after its own helpers
(import ``business_views``, never this module first). Same rules as there: one row per real
thing, plain columns, currency beside every amount, Sage the system of record."""

from __future__ import annotations

import datetime as dt
from typing import Iterator

from operating.pg.business_views import (
    AR_OPEN, CHAIN_COL, ORDER_RECORD_SYSTEM, SAGE_AR_DOC, Reader, Row, Thing, _text,
    add_chain, aging_bucket, customer_chains, customer_link, customer_things, doc_type_doc,
    fill, handle_col, money, pick, to_date, view)

AR_HISTORY_PROPS = ("idinvc", "idcust", "trxtypetxt", "trxtypeid", "dateinvc", "datedue",
                    "datepaid", "amtduetc", "amtinvctc", "codecurn", "swpaid", "idorder",
                    "idcustpo")


@view("receivable_documents", "Every Sage AR document ever booked, paid or open: invoice, "
      "credit note, receipt and the rest, with its date, due date and the date Sage shows it "
      "paid (paid_date, Sage datepaid: the date of the final payment only, so a part payment "
      "has no date of its own; most receipts and prepayments carry none). What is owed today "
      "is customer_balances. Business Central's aged receivables are not in this view.",
      [("customer", "text", "customer name (the linked customer)"),
       ("customer_code", "text", "Sage customer code"),
       ("document_number", "text", "invoice / credit note / receipt number"),
       ("document_type", "text", doc_type_doc(SAGE_AR_DOC, "receipts, prepayments, "
                                              "unapplied cash and credit notes")),
       ("document_date", "date", "document date (Sage dateinvc)"),
       ("due_date", "date", "due date (Sage datedue)"),
       ("paid_date", "date", "the date Sage shows the document fully paid (datepaid); blank "
        "while open, and blank on most receipts and prepayments (paid, no date)"),
       ("status", "text", "open (Sage swpaid 0) or paid (swpaid 1)"),
       ("original_amount", "money", "document amount (Sage amtinvctc), negative for credits "
        "and receipts as Sage states it"),
       ("amount_due", "money", "remaining balance now (Sage amtduetc); 0 when paid"),
       ("order_number", "text", "the sales order it bills, when stated"),
       ("customer_po", "text", "customer PO, when stated"),
       CHAIN_COL,
       handle_col("customer", f"the document's {AR_OPEN}.customer link")],
      reads=(AR_OPEN, "Customer"), memos=("customers", "chains"),
      covers=False)   # every AR document stays in records too
def _receivable_documents(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    docs = rd.records(AR_OPEN, AR_HISTORY_PROPS)
    customers, things = customer_things(rd)
    chains = customer_chains(rd)
    owner = rd.linked(f"{AR_OPEN}.customer", docs, customers)
    for oid in sorted(docs):
        rec = docs[oid]
        paid = rec.cell("swpaid")
        flag = (_text(paid.value) or "") if paid is not None else ""
        if flag not in ("0", "1"):
            continue   # no paid flag stated: neither open nor paid, so no row
        thing = Thing([rec])
        row = Row(thing)
        row.used(rec, paid)
        row.values["status"] = "open" if flag == "0" else "paid"
        fill(row, thing, "customer_code", "idcust")
        fill(row, thing, "document_number", "idinvc")
        kind, krec = pick(thing, "trxtypetxt")
        row.read(kind, "document_type", SAGE_AR_DOC.get((_text(kind.value) or ""), "other")
                 if kind else None, rec=krec)
        fill(row, thing, "document_date", "dateinvc", to_date)
        fill(row, thing, "due_date", "datedue", to_date)
        fill(row, thing, "paid_date", "datepaid", to_date)   # Sage 0 = not paid: no date
        cur = pick(thing, "codecurn")
        money(row, "original_amount", *pick(thing, "amtinvctc"), cur)
        cell, crec = pick(thing, "amtduetc")
        if cell is None:
            cell, crec = rec.cell("amtduetc"), rec
        money(row, "amount_due", cell, crec, cur)
        fill(row, thing, "order_number", "idorder")
        fill(row, thing, "customer_po", "idcustpo")
        add_chain(row, customer_link(row, thing, owner, things, ORDER_RECORD_SYSTEM), chains)
        yield row


from operating.pg import business_views as _bv  # noqa: E402
_bv._register_submodules()
