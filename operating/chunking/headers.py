"""Contextual chunk headers (Anthropic, "Contextual Retrieval", 2024), deterministic.

Each chunk is embedded as ``header + body``. The header states what the bare text
cannot: the source system, the document or thread title, the date, the author's role
(never a name: ``author_role``), the ids the item names or is linked to, and a one-line
"about" written from metadata and links alone. An LLM-written "about" was not adopted:
one model call per chunk over ~10^6 chunks (~600 input tokens each) costs far more than
the whole embedding run and adds a non-deterministic step, while the deterministic line
already carries title, section, thread and links.

A header never says more than the chunk's own reader may read: every value in it is read
at markings the chunk itself carries (the caller drops the others), and a linked object's
label is printed only when the item's own text names it or the object shares the chunk's
audience unit (``label_allowed``).
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from ontology.mentions import mail_domain

SYSTEMS = {"mail": "Email (Outlook)", "attachment": "Email attachment",
           "sharepoint": "SharePoint/OneDrive document", "sheet_row": "Spreadsheet",
           "map_stage": "Process map", "original": "Case document",
           "teams": "Microsoft Teams", "mc_task": "Mission Control task",
           "portal_text": "Customer Portal record", "bc_text": "Business Central record",
           "record_text": "Business record"}
_LABELS = {"petra_po": "PO", "so_number": "SO", "ao_code": "AO", "mo_code": "MO",
           "lot_number": "Lot", "batch_code": "Batch", "item_code": "Item",
           "erp_item_code": "Item", "customer_erp_code": "Customer", "customer_po": "Customer PO",
           "supplier_code": "Supplier", "invoice_number": "Invoice",
           "bc_document_no": "BC doc", "shipment_number": "Shipment",
           "quote_number": "Quote", "receipt_lab_id": "Receipt", "tracking_number": "Tracking",
           "vendor_confirmation": "Vendor confirmation"}
# Identifier kinds that are technical (message ids, hashes, system ids): never a header id.
TECHNICAL = frozenset({"internet_message_id", "content_sha256", "bc_system_id", "fm_id",
                       "sage_ord_uniq", "project_key"})


def id_label(kind: str, value: str) -> str:
    return f"{_LABELS.get(kind, kind.replace('_', ' '))} {value}"


def sender_role(sender: str | None, sender_class: str | None) -> str:
    """The role a mail sender is shown by: internal staff, or an external party by its
    domain (an organisation, not a person); never the name or address."""
    domain = mail_domain(sender) if sender else None
    if sender_class == "internal":
        return "internal staff"
    if sender_class == "external":
        return f"external ({domain})" if domain else "external sender"
    return f"sender at {domain} (unverified)" if domain else "unknown sender"


def label_allowed(label: str, text: str, same_audience: bool) -> bool:
    if same_audience:
        return True
    folded = " ".join(text.casefold().split())
    label = " ".join(label.casefold().split())
    # A whole token run only: "PO12" is not named by "PO123" or "XPO12".
    return len(label) >= 3 and re.search(
        r"(?<![0-9a-z])" + re.escape(label) + r"(?![0-9a-z])", folded) is not None


def _clip(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def build(*, kind: str, title: str | None, date: str | None, author_role: str | None,
          ids: Sequence[str], names: Iterable[str] = (), about: str | None,
          where: str | None = None) -> str:
    parts = [f"Source: {SYSTEMS.get(kind, kind)}"]
    if title := _clip(title):
        parts.append(f"Title: {title}")
    if where := _clip(where, 200):
        parts.append(f"Where: {where}")
    if date:
        parts.append(f"Date: {date[:10]}")
    if author_role:
        parts.append(f"Author: {author_role}")
    shown = list(dict.fromkeys(ids))[:12]
    if shown:
        parts.append("Ids: " + ", ".join(shown))
    named = [n for n in dict.fromkeys(_clip(n, 80) for n in names) if n][:6]
    if named:
        parts.append("Parties/items: " + "; ".join(named))
    if about := _clip(about, 240):
        parts.append(f"About: {about}")
    return "\n".join(parts)


_WS = re.compile(r"\s+")


def one_line(text: str, limit: int = 120) -> str:
    return _clip(_WS.sub(" ", text), limit) or ""


__all__ = ["SYSTEMS", "TECHNICAL", "build", "id_label", "label_allowed", "one_line",
           "sender_role"]
