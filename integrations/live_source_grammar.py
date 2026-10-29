"""Closed request grammar for live Microsoft sources (Graph mail/SharePoint/Teams, BC).

Every credentialed GET URL is built from one fixed template per operation (Graph
v1.0; beta only for ``graph.list_item_permissions_beta``) with typed parameters, and
``live_source_guard.check_request`` accepts a GET only if its path parses back into
one of these templates with every parameter passing the same validator. No free
string is ever concatenated into a path; a caller-shaped value either is a
well-formed typed parameter or the request is refused before I/O.

Parameter types (validated on the *decoded* value):

* ``user_ref``   — object id (GUID) or UPN (strict mailbox grammar). Used only
  for the directory lookup that resolves the canonical identity.
* ``user_id``    — GUID (the resolved object id; every mailbox read uses it).
* ``mail_id``    — Graph message/folder id: ``[A-Za-z0-9_=-]`` only (no ``/``,
  ``.``, ``%``, ``\\``).
* ``mail_folder``— an enumerated well-known folder name or a ``mail_id``.
* ``drive_id`` / ``item_id`` — ``[A-Za-z0-9_=!-]`` only.
* ``site_id``    — ``<tenant>.sharepoint.com,<guid>,<guid>`` or a GUID.
* ``sp_host`` / ``site_name`` — ``<tenant>.sharepoint.com`` and one plain site
  URL name (``[A-Za-z0-9][A-Za-z0-9_-]*``), for ``graph.site_by_path`` only.
* ``drive_path`` — ``/seg/seg``; each segment non-empty, no control
  characters, ``/``, ``\\``, ``%``, ``:``, ``..``, leading/trailing space or
  trailing dot (so ``Report..v2.pdf`` cannot be addressed by path; use its id).
* ``tenant`` (GUID or DNS name), ``environment``, ``guid``,
  ``entity_set`` (allowlist below).
* ``share_id`` — an encoded sharing URL, ``u!`` + base64url (no padding) of a
  plain ``https://`` URL (Microsoft Graph "Encoding sharing URLs"), for
  ``graph.share_drive_item`` only; ``s!`` tokens and raw share ids are refused.
* ``search_text`` — one search term for ``graph.drive_search`` (and a mail
  ``$search``): letters, digits and inner ``# . _ -``, at most 64 characters,
  starting and ending with a letter or digit (no quote, space, ``/`` or ``..``).
* ``teams_thread`` — a Teams channel or chat thread id
  (``19:<id>@thread.tacv2|thread.v2|thread.skype|unq.gbl.spaces``, ``<id>`` a
  ``mail_id``-grammar Graph id); ``teams_message_id`` — a chat message id
  (``mail_id`` grammar). A team is addressed by its group id (``guid``).

Beyond the template match the guard decodes every path segment once and
refuses any ``/``, ``\\``, ``%`` or ``..`` component after decoding.
"""

from __future__ import annotations

import base64
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, quote, unquote, urlencode

from ontology import identifiers as _ids


GRAPH = "graph.microsoft.com"
BC = "api.businesscentral.dynamics.com"
# External read-only sources: the Freightcom
# Customer API *test* host only (the production host external-api.freightcom.com is named
# by no decision and stays outside the allowlist) and the Amazon SP-API regional hosts.
FREIGHTCOM_TEST = "customer-external-api.ssd-test.freightcom.com"
SP_API_HOSTS: dict[str, str] = {"na": "sellingpartnerapi-na.amazon.com",
                                "eu": "sellingpartnerapi-eu.amazon.com",
                                "fe": "sellingpartnerapi-fe.amazon.com"}
EXTERNAL_HOSTS = frozenset({FREIGHTCOM_TEST, *SP_API_HOSTS.values()})

# Identifier grammars (ontology/schema/identifiers.json ``system_kinds``); every
# validator below uses ``fullmatch``.
_GUID = _ids.system_pattern("guid")
_UPN = _ids.system_pattern("upn")
_MAIL_ID = _ids.system_pattern("graph_mail_id")
_DRIVE_ID = _ids.system_pattern("graph_drive_id")
_SHARE_ID = _ids.system_pattern("graph_share_id")
_SEARCH_TEXT = _ids.system_pattern("graph_search_text")
_SITE_ID = _ids.system_pattern("sharepoint_site_id")
_TENANT = _ids.system_pattern("entra_tenant")
_ENVIRONMENT = _ids.system_pattern("bc_environment")
WELL_KNOWN_MAIL_FOLDERS = frozenset({
    "inbox", "sentitems", "drafts", "deleteditems", "archive", "junkemail", "outbox",
    "clutter", "conversationhistory", "recoverableitemsdeletions", "scheduled",
    "searchfolders", "syncissues", "msgfolderroot"})
# BC API v2.0 read entity sets the Workbench may name (no employees/payroll).
BC_ENTITY_SETS = frozenset({
    "companies", "companyInformation", "currencyExchangeRates",
    "customers", "vendors", "items", "contacts", "locations", "currencies",
    "unitsOfMeasure", "paymentTerms", "paymentMethods", "shipmentMethods", "itemCategories",
    "countriesRegions", "salesQuotes", "salesQuoteLines", "salesOrders", "salesOrderLines",
    "salesInvoices", "salesInvoiceLines", "salesShipments", "salesShipmentLines",
    "salesCreditMemos", "salesCreditMemoLines", "purchaseOrders", "purchaseOrderLines",
    "purchaseInvoices", "purchaseInvoiceLines", "purchaseReceipts", "purchaseReceiptLines",
    "purchaseCreditMemos", "purchaseCreditMemoLines", "itemLedgerEntries",
    "generalLedgerEntries", "accounts", "dimensions", "dimensionValues",
    "agedAccountsReceivables", "agedAccountsPayables", "customerPaymentJournals",
    "customerPayments", "vendorPaymentJournals", "vendorPayments"})
# BC document header set -> its line set, read only through the nested
# navigation ``<header>(<id>)/<lines>`` (one literal template per pair).
BC_DOCUMENT_LINES: dict[str, str] = {
    "purchaseOrders": "purchaseOrderLines", "purchaseReceipts": "purchaseReceiptLines",
    "purchaseInvoices": "purchaseInvoiceLines", "salesOrders": "salesOrderLines",
    "salesInvoices": "salesInvoiceLines", "salesCreditMemos": "salesCreditMemoLines",
    "purchaseCreditMemos": "purchaseCreditMemoLines", "salesQuotes": "salesQuoteLines",
    "customerPaymentJournals": "customerPayments", "vendorPaymentJournals": "vendorPayments"}
# The field a line names its header by (BC API v2.0): document lines ``documentId``,
# payment journal lines ``journalId``, a dimension's values ``dimensionId`` (live
# $metadata pin 09a82605). A line naming another header is dropped.
BC_LINE_PARENT_FIELD: dict[str, str] = {"customerPayments": "journalId",
                                        "vendorPayments": "journalId",
                                        "dimensionValues": "dimensionId"}
# Entity sets keyed by another field than ``id`` (BC API v2.0 report pages keyed by the
# party): the record id, the citation and a get-by-id use that field.
BC_RECORD_KEY_FIELD: dict[str, str] = {"agedAccountsReceivables": "customerId",
                                       "agedAccountsPayables": "vendorId"}
# Teams thread ids (channels and chats) as Graph returns them (``channelIdentity.
# channelId``, ``chatId``): ``19:<opaque>@<suffix>``, the opaque part in the shared
# Graph id grammar and the suffix one of these. A chat message id is a Graph message
# id (``graph_mail_id``).
TEAMS_THREAD_SUFFIXES = frozenset({"thread.tacv2", "thread.v2", "thread.skype",
                                   "unq.gbl.spaces"})


def _teams_thread_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("19:"):
        return False
    body, at, suffix = value[3:].rpartition("@")
    return (bool(at) and suffix in TEAMS_THREAD_SUFFIXES and len(body) <= 256
            and _MAIL_ID.fullmatch(body) is not None)


_DRIVE_SEGMENT_BAD = re.compile(r"[\x00-\x1f\x7f/\\%:*?\"<>|#]")


def _drive_segment_ok(segment: str) -> bool:
    return (bool(segment) and len(segment) <= 255 and ".." not in segment
            and segment == segment.strip() and not segment.endswith(".")
            and _DRIVE_SEGMENT_BAD.search(segment) is None)


def _drive_path_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 1024:
        return False
    return all(_drive_segment_ok(s) for s in value[1:].split("/"))




def encode_share_id(sharing_url: str) -> str:
    """``u!`` + unpadded base64url of a sharing URL (Graph ``/shares/{id}`` form)."""
    return "u!" + base64.urlsafe_b64encode(sharing_url.encode("utf-8")).decode(
        "ascii").rstrip("=")




def search_text_ok(value: Any) -> bool:
    """One search term (``search_text``): a document number, never a query expression."""
    return (isinstance(value, str) and _SEARCH_TEXT.fullmatch(value) is not None
            and ".." not in value)


def _share_id_ok(value: Any) -> bool:
    if not isinstance(value, str) or _SHARE_ID.fullmatch(value) is None:
        return False
    body = value[2:]
    try:
        url = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return (url.startswith("https://") and encode_share_id(url) == value
            and not any(ord(c) <= 32 or ord(c) == 127 for c in url))


# BC company *names* (ODataV4 web-service pages address a company by name, never by
# GUID): only a name the pinned book map lists (``bc_company_books.json`` ``names``) and
# only in a plain shape (no quote, parenthesis, slash, percent or control character), so
# the ``Company('<name>')`` key literal never needs escaping. An unreadable map pins none.
_BC_COMPANY_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._&-]{0,62}[A-Za-z0-9.])?")
_BOOKS_FILE = Path(__file__).with_name("bc_company_books.json")


def _pinned_company_names() -> frozenset[str]:
    try:
        names = json.loads(_BOOKS_FILE.read_text(encoding="utf-8")).get("names", {})
        return frozenset(name for pins in names.values() if isinstance(pins, dict)
                         for name in pins.values() if isinstance(name, str))
    except (OSError, ValueError, AttributeError):
        return frozenset()


BC_PINNED_COMPANY_NAMES = _pinned_company_names()


def bc_company_name_ok(value: Any) -> bool:
    return (isinstance(value, str) and _BC_COMPANY_NAME.fullmatch(value) is not None
            and "  " not in value and value in BC_PINNED_COMPANY_NAMES)


class ParamType:
    def __init__(self, name: str, valid: Callable[[Any], bool], group: str,
                 encode: Callable[[str], str]) -> None:
        self.name, self.valid, self.group, self.encode = name, valid, group, encode


def _rx(pattern: re.Pattern) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and pattern.fullmatch(v) is not None


_SEG = r"[^/()]+"
_VERSION_ID = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}")
# Site-by-path resolution (``graph.site_by_path``): a SharePoint host and one
# site URL name under ``/sites/``. The host is the configured site's host
# (``SHAREPOINT_SITE_ID``), never a caller string; the name is a plain URL name.
_SP_HOST = _ids.system_pattern("sharepoint_host")
_SITE_NAME = _ids.system_pattern("sharepoint_site_name")
TYPES: dict[str, ParamType] = {t.name: t for t in (
    ParamType("user_ref", lambda v: _rx(_GUID)(v) or _rx(_UPN)(v), _SEG,
              lambda v: quote(v, safe="@")),
    ParamType("user_id", _rx(_GUID), _SEG, lambda v: v.lower()),
    ParamType("mail_id", _rx(_MAIL_ID), _SEG, lambda v: v),
    ParamType("mail_folder", lambda v: (isinstance(v, str) and v.lower()
                                        in WELL_KNOWN_MAIL_FOLDERS) or _rx(_MAIL_ID)(v),
              _SEG, lambda v: v),
    ParamType("drive_id", _rx(_DRIVE_ID), _SEG, lambda v: v),
    ParamType("item_id", _rx(_DRIVE_ID), _SEG, lambda v: v),
    # A SharePoint file version label (``major.minor``, e.g. ``195.0``).
    ParamType("version_id", _rx(_VERSION_ID), _SEG, lambda v: v),
    ParamType("site_id", lambda v: _rx(_GUID)(v) or _rx(_SITE_ID)(v), _SEG, lambda v: v),
    ParamType("drive_path", _drive_path_ok, r"(?:/[^/:]+)+",
              lambda v: "".join("/" + quote(s, safe="") for s in v[1:].split("/"))),
    ParamType("sp_host", _rx(_SP_HOST), r"[^/():]+", lambda v: v),
    ParamType("site_name", _rx(_SITE_NAME), _SEG, lambda v: v),
    ParamType("tenant", _rx(_TENANT), _SEG, lambda v: v),
    ParamType("environment", _rx(_ENVIRONMENT), _SEG, lambda v: v),
    ParamType("guid", _rx(_GUID), r"[^/()]+", lambda v: v.lower()),
    ParamType("bc_company_name", bc_company_name_ok, r"[^/()']+",
              lambda v: quote(v, safe="")),
    ParamType("entity_set", lambda v: isinstance(v, str) and v in BC_ENTITY_SETS
              and v != "companies", r"[^/()]+", lambda v: v),
    ParamType("share_id", _share_id_ok, _SEG, lambda v: v),
    ParamType("teams_thread", _teams_thread_ok, _SEG, lambda v: v),
    ParamType("search_text", search_text_ok, r"[^/()']+", lambda v: quote(v, safe="")),
    ParamType("teams_message_id", _rx(_MAIL_ID), _SEG, lambda v: v),
    ParamType("freightcom_id", _rx(_ids.system_pattern("freightcom_id")), _SEG, lambda v: v),
    ParamType("amazon_shipment_id", _rx(_ids.system_pattern("amazon_shipment_id")), _SEG,
              lambda v: v),
)}

_BC = "/v2.0/{tenant:tenant}/{environment:environment}/api/v2.0"
_CHANNEL = "/v1.0/teams/{team:guid}/channels/{channel:teams_thread}"
TEMPLATES: dict[str, tuple[str, str]] = {
    "graph.users": (GRAPH, "/v1.0/users"),
    "graph.user": (GRAPH, "/v1.0/users/{user:user_ref}"),
    # An employee's transitive group membership, cast to groups (directory
    # roles and administrative units excluded): integrations.graph_directory.
    # GET; ``getMemberGroups``/``checkMemberGroups`` are POSTs and never used.
    "graph.user_transitive_groups": (GRAPH, "/v1.0/users/{user:user_id}/transitiveMemberOf/"
                                     "microsoft.graph.group"),
    "graph.messages": (GRAPH, "/v1.0/users/{user:user_id}/messages"),
    "graph.message": (GRAPH, "/v1.0/users/{user:user_id}/messages/{message:mail_id}"),
    "graph.mail_folder": (GRAPH, "/v1.0/users/{user:user_id}/mailFolders/{folder:mail_folder}"),
    # A mailbox's top-level folders and one folder's child folders (ids and item counts):
    # the mail capture's every-folder scope (integrations.capture_mail, folders "all").
    "graph.mail_folders": (GRAPH, "/v1.0/users/{user:user_id}/mailFolders"),
    "graph.mail_child_folders": (GRAPH, "/v1.0/users/{user:user_id}/mailFolders/"
                                 "{folder:mail_id}/childFolders"),
    "graph.folder_messages": (GRAPH,
                              "/v1.0/users/{user:user_id}/mailFolders/{folder:mail_id}/messages"),
    "graph.site": (GRAPH, "/v1.0/sites/{site:site_id}"),
    "graph.site_drives": (GRAPH, "/v1.0/sites/{site:site_id}/drives"),
    # Every site the application may read (app-only ``Sites.Read.All``): the
    # SharePoint estate discovery of integrations.capture_sharepoint.
    "graph.sites_all": (GRAPH, "/v1.0/sites/getAllSites"),
    # Site by server-relative path (``GET /sites/{hostname}:/sites/{name}``; no
    # trailing colon: nothing is addressed below the site) and a site's default
    # document library. Used to resolve a configured site name to its drive.
    "graph.site_by_path": (GRAPH, "/v1.0/sites/{host:sp_host}:/sites/{name:site_name}"),
    "graph.site_drive": (GRAPH, "/v1.0/sites/{site:site_id}/drive"),
    # A drive's own metadata (``$select=id,name,driveType``): the library name the
    # SharePoint capture classifies a named drive target by.
    "graph.drive": (GRAPH, "/v1.0/drives/{drive:drive_id}"),
    "graph.drive_root": (GRAPH, "/v1.0/drives/{drive:drive_id}/root"),
    "graph.drive_path": (GRAPH, "/v1.0/drives/{drive:drive_id}/root:{path:drive_path}:"),
    "graph.drive_item": (GRAPH, "/v1.0/drives/{drive:drive_id}/items/{item:item_id}"),
    "graph.drive_children": (GRAPH, "/v1.0/drives/{drive:drive_id}/items/{item:item_id}/children"),
    # Original bytes of one item (integrations.shared_link): Graph answers 302 with a
    # pre-authenticated Location, fetched credential-free (``SourceApi.get_content``).
    "graph.drive_item_content": (GRAPH,
                                 "/v1.0/drives/{drive:drive_id}/items/{item:item_id}/content"),
    # Bytes of one named historical version of an item (a pinned revision that is no
    # longer current, scripts.case_originals): the same 302 hand-back as the item's own
    # content route. No version listing or restore path parses.
    "graph.drive_item_version_content": (GRAPH, "/v1.0/drives/{drive:drive_id}/items/"
                                         "{item:item_id}/versions/{version:version_id}/content"),
    # A SharePoint/OneDrive sharing URL resolved to the item it names; no other
    # ``/shares/`` sub-path parses.
    "graph.share_drive_item": (GRAPH, "/v1.0/shares/{share:share_id}/driveItem"),
    "graph.drive_permissions": (GRAPH,
                                "/v1.0/drives/{drive:drive_id}/items/{item:item_id}/permissions"),
    # Delta/evidence readers (integrations.graph_evidence). The mail delta
    # continuation form ``mailFolders('<id>')`` is what Graph hands back in
    # nextLink/deltaLink, so both spellings of the one collection parse.
    "graph.folder_messages_delta": (GRAPH, "/v1.0/users/{user:user_id}/mailFolders/"
                                    "{folder:mail_folder}/messages/delta"),
    "graph.folder_messages_delta_keyed": (GRAPH, "/v1.0/users/{user:user_id}/mailFolders("
                                          "'{folder:mail_folder}')/messages/delta"),
    "graph.message_attachments": (GRAPH, "/v1.0/users/{user:user_id}/messages/"
                                  "{message:mail_id}/attachments"),
    "graph.message_attachment": (GRAPH, "/v1.0/users/{user:user_id}/messages/"
                                 "{message:mail_id}/attachments/{attachment:mail_id}"),
    "graph.drive_delta": (GRAPH, "/v1.0/drives/{drive:drive_id}/root/delta"),
    # Drive search for one term (the oracle's root-first document strata,
    # scripts/eval_oracle_dev.py LiveDocuments.search): a function call on the root,
    # GET; each hit is then opened through the reader's audience and ACL check.
    "graph.drive_search": (GRAPH, "/v1.0/drives/{drive:drive_id}/root/search(q='{q:search_text}')"),
    "graph.drive_list_item": (GRAPH,
                              "/v1.0/drives/{drive:drive_id}/items/{item:item_id}/listItem"),
    # A document library's backing list, its column definitions and its list items
    # (counts of filled mirrored columns only; SharePointReader.library_columns). The
    # list and column reads are the Portal's own address forms (plx-customer-portal
    # scripts/document-metadata/provision-sharepoint-columns.mjs resolveList,
    # portal/src/lib/m365/sharepoint.ts getListColumns): ``/drives/{d}/list`` names the
    # Graph site id and list id the column read uses. The items are addressed through
    # the drive itself, so no site or list id can point the item read at another list.
    "graph.drive_list": (GRAPH, "/v1.0/drives/{drive:drive_id}/list"),
    "graph.list_columns": (GRAPH, "/v1.0/sites/{site:site_id}/lists/{list:guid}/columns"),
    "graph.drive_list_items": (GRAPH, "/v1.0/drives/{drive:drive_id}/list/items"),
    # A SharePoint list a capture scope names by site and list id
    # (integrations.capture_sharepoint_lists): the list itself (that it is a list, not a
    # document library), its items with their column values, and each item's permissions.
    "graph.site_list": (GRAPH, "/v1.0/sites/{site:site_id}/lists/{list:guid}"),
    "graph.list_items": (GRAPH, "/v1.0/sites/{site:site_id}/lists/{list:guid}/items"),
    "graph.list_item_permissions": (GRAPH, "/v1.0/sites/{site:site_id}/lists/{list:guid}/items/"
                                    "{item:item_id}/permissions"),
    # The documented beta form of the same per-item read (Sites.Read.All app), read only
    # after the v1.0 one fails. The grammar's one beta path: no other beta operation, and no
    # list- or site-wide permissions path in either version, parses.
    "graph.list_item_permissions_beta": (GRAPH, "/beta/sites/{site:site_id}/lists/{list:guid}/"
                                         "items/{item:item_id}/permissions"),
    # Teams capture (integrations.capture_teams), GET only: conversation discovery
    # from an allowlisted member, membership (the captured items' audience), channel
    # message delta with the replies of each root message, chat messages.
    "graph.user_joined_teams": (GRAPH, "/v1.0/users/{user:user_id}/joinedTeams"),
    "graph.user_chats": (GRAPH, "/v1.0/users/{user:user_id}/chats"),
    "graph.team_channels": (GRAPH, "/v1.0/teams/{team:guid}/channels"),
    "graph.team_channel": (GRAPH, "/v1.0/teams/{team:guid}/channels/{channel:teams_thread}"),
    "graph.team_members": (GRAPH, "/v1.0/teams/{team:guid}/members"),
    "graph.channel_members": (GRAPH, _CHANNEL + "/members"),
    "graph.channel_messages_delta": (GRAPH, _CHANNEL + "/messages/delta"),
    # One page of a channel's messages (``$top=1``), read only to tell an empty channel
    # from a failure when Graph refuses its own delta nextLink (``capture_teams``).
    "graph.channel_messages": (GRAPH, _CHANNEL + "/messages"),
    "graph.channel_message_replies": (GRAPH, _CHANNEL + "/messages/"
                                      "{message:teams_message_id}/replies"),
    "graph.chat_members": (GRAPH, "/v1.0/chats/{chat:teams_thread}/members"),
    "graph.chat_messages": (GRAPH, "/v1.0/chats/{chat:teams_thread}/messages"),
    # The Teams census (``capture_teams.TeamsCapture.census``): every team, one team's
    # and one chat's own record (names, read only once its members cleared the people
    # guard) and the tenant's 30-day per-team activity report (counts, no content).
    "graph.teams": (GRAPH, "/v1.0/teams"),
    "graph.team": (GRAPH, "/v1.0/teams/{team:guid}"),
    "graph.chat": (GRAPH, "/v1.0/chats/{chat:teams_thread}"),
    "graph.teams_activity_report": (GRAPH, "/v1.0/reports/"
                                    "getTeamsTeamActivityDetail(period='D30')"),
    "bc.companies": (BC, _BC + "/companies"),
    # The API v2.0 schema document (CSDL XML; ``SourceApi.get_document``, no query).
    "bc.metadata": (BC, _BC + "/$metadata"),
    "bc.entities": (BC, _BC + "/companies({company:guid})/{entity:entity_set}"),
    "bc.entity": (BC, _BC + "/companies({company:guid})/{entity:entity_set}({record:guid})"),
    # The one ODataV4 web-service read (live check ``bc.purchase_reader``): Page 50 ``PurchaseOrder`` serves ``Your_Reference``
    # (the FileMaker PO number), which API v2.0 ``purchaseOrders`` does not expose. Only
    # this entity set, only by a pinned company name, only the query
    # ``bc_odata_purchase_order_query`` builds; no key, navigation or action ever.
    "bc.odata_purchase_orders": (BC, "/v2.0/{tenant:tenant}/{environment:environment}"
                                 "/ODataV4/Company('{company:bc_company_name}')/PurchaseOrder"),
    **{f"bc.lines.{parent}": (BC, _BC + "/companies({company:guid})/" + parent
                              + "({record:guid})/" + lines)
       for parent, lines in BC_DOCUMENT_LINES.items()},
    # Freightcom Customer API 2.x, GET only (rating and booking are POSTs, never sent).
    # Paths beyond /rate (the Portal's client, plx-customer-portal@efd46d3:portal/src/lib/
    # wms/carrier/freightcom/client.ts) are unverified until freightcom.bookings passes.
    "freightcom.services": (FREIGHTCOM_TEST, "/services"),
    "freightcom.shipments": (FREIGHTCOM_TEST, "/shipments"),
    "freightcom.shipment": (FREIGHTCOM_TEST, "/shipment/{shipment:freightcom_id}"),
    "freightcom.shipment_tracking": (FREIGHTCOM_TEST,
                                     "/shipment/{shipment:freightcom_id}/tracking-events"),
    # Amazon SP-API, GET only, one set per region host (sp_api.<region>.<operation>).
    **{f"sp_api.{region}.{name}": (host, path)
       for region, host in SP_API_HOSTS.items()
       for name, path in (
           ("inbound_shipments", "/fba/inbound/v0/shipments"),
           ("inbound_shipment_items",
            "/fba/inbound/v0/shipments/{shipment:amazon_shipment_id}/items"),
           ("inbound_labels",
            "/fba/inbound/v0/shipments/{shipment:amazon_shipment_id}/labels"),
           ("inventory_summaries", "/fba/inventory/v1/summaries"),
           ("order_metrics", "/sales/v1/orderMetrics"))},
}
# Graph silently ignores instance-annotation terms in $select on drive items
# (``$select=id,@microsoft.graph.downloadUrl`` answers 200 without the URL,
# live 2026-10-01 on PLXCUSTOMERS). The download URL is requested through the
# ``content.downloadUrl`` property instead and still arrives under the
# annotation key. Only a drive-item read made after the audience decision may
# carry this select term.
DRIVE_DOWNLOAD_URL_SELECT = "content.downloadUrl"
DRIVE_DOWNLOAD_URL_KEY = "@microsoft.graph.downloadUrl"


def drive_item_select(base: str, *, download_url: bool) -> str:
    """A drive-item ``$select`` value, with the download URL term only on request."""
    if not isinstance(base, str) or not base or "downloadurl" in base.lower():
        raise ValueError("drive item select must name plain properties only")
    return base + ("," + DRIVE_DOWNLOAD_URL_SELECT if download_url else "")


# ------------------------------------------------------------------ listItem fields
#
# A document-library file's SharePoint columns are read through the file's own
# listItem relationship, never by reconstructing a site/list address
# (``sharepointIds.siteId`` is an SPSite GUID, not a Graph site id):
# ``GET /v1.0/drives/{drive}/items/{item}/listItem?expand=fields``. The URL is
# built with every id percent-encoded (``quote(..., safe="")``), so the request
# URL is byte-for-byte the ``source_api`` that operating/doc_metadata.py requires
# of a stored or supplied listItem snapshot. It still parses into the closed template ``graph.drive_list_item``.
LIST_ITEM_FIELDS_QUERY = "expand=fields"


def column_name_ok(value: Any) -> bool:
    """A plain SharePoint internal column name (no spaces, no ``@odata.`` keys)."""
    return _ids.valid("sharepoint_column_name", value)


def drive_list_item_fields_url(drive_id: Any, item_id: Any) -> str:
    """The one listItem-fields URL for a drive item; ValueError on any bad id."""
    if not TYPES["drive_id"].valid(drive_id) or not TYPES["item_id"].valid(item_id):
        raise ValueError("graph.drive_list_item: drive or item id invalid")
    path = ("/v1.0/drives/" + quote(drive_id, safe="") + "/items/" +
            quote(item_id, safe="") + "/listItem")
    if match(GRAPH, path) != "graph.drive_list_item" or not segments_clean(path):
        raise ValueError("graph.drive_list_item: url does not parse back")
    return f"https://{GRAPH}{path}?{LIST_ITEM_FIELDS_QUERY}"


def drive_list_item_fields_url_ok(url: Any, drive_id: Any, item_id: Any) -> bool:
    """Whether ``url`` is exactly the listItem-fields read of this drive's item."""
    try:
        return isinstance(url, str) and url == drive_list_item_fields_url(drive_id, item_id)
    except ValueError:
        return False


_FIELD = re.compile(r"\{([a-z]+):([a-z_]+)\}")


def _compile(template: str) -> tuple[re.Pattern, list[tuple[str, str]]]:
    fields: list[tuple[str, str]] = []
    pattern, last = "", 0
    for match in _FIELD.finditer(template):
        pattern += re.escape(template[last:match.start()])
        fields.append((match[1], match[2]))
        pattern += f"(?P<{match[1]}>{TYPES[match[2]].group})"
        last = match.end()
    pattern += re.escape(template[last:])
    return re.compile("^" + pattern + "$", re.IGNORECASE), fields


_COMPILED = {op: (host, *_compile(template)) for op, (host, template) in TEMPLATES.items()}


def url(op: str, query: Mapping[str, str] | None = None, **params: str) -> str:
    """Build the one URL an operation may send; ValueError on any bad parameter."""
    if op not in TEMPLATES:
        raise ValueError("unknown operation")
    host, template = TEMPLATES[op]
    _pattern, fields = _COMPILED[op][1], _COMPILED[op][2]
    if set(params) != {name for name, _t in fields}:
        raise ValueError("operation parameters mismatch")
    path = template
    for name, type_name in fields:
        kind = TYPES[type_name]
        if not kind.valid(params[name]):
            raise ValueError(f"{op}: parameter {name} is not a valid {type_name}")
        path = path.replace("{" + f"{name}:{type_name}" + "}", kind.encode(params[name]))
    result = f"https://{host}{path}"
    if query:
        result += "?" + urlencode(dict(query))
    return result


def match(host: str, path: str) -> str | None:
    """The operation whose template this (encoded) path parses into, else None."""
    for op, (op_host, pattern, fields) in _COMPILED.items():
        if op_host != host:
            continue
        found = pattern.fullmatch(path)
        if found is None:
            continue
        ok = True
        for name, type_name in fields:
            raw = found.group(name)
            if type_name == "drive_path":
                value = "".join("/" + unquote(s) for s in raw[1:].split("/"))
            else:
                value = unquote(raw)
            if not TYPES[type_name].valid(value):
                ok = False
                break
        if ok:
            return op
    return None


def segments_clean(path: str) -> bool:
    """Every segment decoded once carries no '/', '\\', '%', '..' or control char."""
    for segment in path.split("/")[1:]:
        decoded = unquote(segment)
        if ("/" in decoded or "\\" in decoded or "%" in decoded or ".." in decoded
                or decoded == "." or any(ord(c) < 32 or ord(c) == 127 for c in decoded)):
            return False
    return True


# ------------------------------------------------------------------ BC queries
#
# The only OData query options a BC read may carry. ``$filter`` is exactly one
# equality on an allowlisted field of that entity set, built here from a
# validated value (never a caller-written expression), or the one typed
# ``lastModifiedDateTime ge <instant>`` form on ``BC_MODIFIED_SINCE_SETS``;
# ``$top`` is bounded; ``$select`` names come from the entity set's allowlist
# (or are exactly ``id,lastModifiedDateTime`` on those sets). Entity sets without
# a spec accept only ``$top`` and, if listed there, the modified-since forms.
# ``$expand`` is exactly
# the line set of a document header set (``BC_DOCUMENT_LINES``: ``purchaseOrders`` expands
# ``purchaseOrderLines`` and nothing else), never on a line set, never with ``$select``.

BC_MAX_TOP = 1000
# Document / master numbers (BC Code[20]; external document numbers up to 35):
# letters, digits, inner single spaces and ``. _ / # -`` (no trailing space). No
# quote, so the OData string literal never needs escaping. Grammar:
# ``bc_number_literal`` in ontology/schema/identifiers.json.
def bc_number_ok(value: Any) -> bool:
    return _ids.valid("bc_number_literal", value)


class BcQuerySpec:
    """``filters``: field -> ``"number"`` (quoted literal) or ``"guid"`` (bare).
    ``select``: allowlisted ``$select`` names. ``always``: names added to every
    ``$select`` so identity and provenance survive projection. ``orderby``: fields
    a listing may be sorted by (``$orderby=<field> asc|desc``, one field only)."""

    def __init__(self, filters: Mapping[str, str], select: frozenset[str],
                 always: frozenset[str], orderby: frozenset[str] = frozenset()) -> None:
        self.filters, self.select, self.always = dict(filters), select, always
        self.orderby = orderby


def _spec(filters: Mapping[str, str], select: str, *, line: bool = False,
          orderby: str = "", key: str = "id") -> BcQuerySpec:
    """A document line set also filters by ``documentId`` (the flat line read a failed
    nested navigation falls back to, ``BusinessCentralReader.list_lines``). A report page
    keyed by its party (``key``: the aged balances) has no ``lastModifiedDateTime``."""
    always = ({key, "documentId"} if line else
              {key, "lastModifiedDateTime"} if key == "id" else {key})
    names = frozenset(select.split()) | always
    return BcQuerySpec({key: "guid", **({"documentId": "guid"} if line else {}), **filters},
                       names, frozenset(always), frozenset(orderby.split()))


_HEADER_MONEY = ("currencyCode pricesIncludeTax discountAmount totalAmountExcludingTax "
                 "totalTaxAmount totalAmountIncludingTax status")
_LINE = ("sequence itemId lineType lineObjectNumber description unitOfMeasureCode quantity "
         "discountAmount discountPercent amountExcludingTax amountIncludingTax")
BC_QUERY_SPECS: dict[str, BcQuerySpec] = {
    "purchaseOrders": _spec({"number": "number"},
        "number orderDate postingDate vendorId vendorNumber vendorName payToVendorId "
        "payToVendorNumber currencyId paymentTermsId shipmentMethodId purchaser "
        "requestedReceiptDate fullyReceived " + _HEADER_MONEY),
    "purchaseOrderLines": _spec({}, _LINE + " directUnitCost expectedReceiptDate "
                                "receivedQuantity invoicedQuantity", line=True),
    # orderNumber: Portal reads posted receipts back by ``orderNumber eq '<BC PO No.>'``
    # (plx-customer-portal portal/src/lib/business-central/outbox-drain.ts@66d7e8b:1196,
    # bc_receipt_snapshot.order_number); unverified by our live probe until
    # bc.case_entities / bc.purchase_reader report it.
    # orderby (newest first): Portal sorts BC posted documents by ``postingDate desc``
    # (outbox-drain.ts@66d7e8b:712, salesShipments); unverified live for these sets
    # until eval.unfamiliar_sample runs.
    "purchaseReceipts": _spec({"number": "number", "orderNumber": "number"},
        "number postingDate vendorNumber vendorName currencyCode orderNumber",
        orderby="postingDate number"),
    "purchaseReceiptLines": _spec({}, "sequence lineType lineObjectNumber description "
                                  "unitOfMeasureCode unitCost quantity expectedReceiptDate",
                                  line=True),
    # orderId: the PO's id, the key PurchaseInvoice.invoices joins on, so a read scoped to
    # one PO (integrations.bc_purchase, scoped_completeness) covers that link exactly.
    # Unverified live until bc.purchase_reader passes with it.
    "purchaseInvoices": _spec({"number": "number", "vendorInvoiceNumber": "number",
                               "orderNumber": "number", "orderId": "guid"},
        "number invoiceDate postingDate dueDate vendorInvoiceNumber vendorId vendorNumber "
        "vendorName orderId orderNumber " + _HEADER_MONEY),
    "purchaseInvoiceLines": _spec({}, _LINE + " unitCost", line=True),
    # orderby orderDate: REC-10 samples the newest sales orders first
    # (operating.reconciliation_rules.SAMPLE_ORDERBY); unverified live until
    # case.rec10_external_doc runs with it.
    "salesOrders": _spec({"number": "number", "externalDocumentNumber": "number"},
        "number externalDocumentNumber orderDate postingDate customerId customerNumber "
        "customerName requestedDeliveryDate fullyShipped " + _HEADER_MONEY,
        orderby="orderDate"),
    "salesOrderLines": _spec({}, _LINE + " unitPrice shipmentDate shippedQuantity "
                             "invoicedQuantity", line=True),
    "salesInvoices": _spec({"number": "number", "externalDocumentNumber": "number",
                            "orderNumber": "number"},
        "number externalDocumentNumber invoiceDate postingDate dueDate customerId "
        "customerNumber customerName orderId orderNumber " + _HEADER_MONEY,
        orderby="postingDate invoiceDate number"),
    "salesInvoiceLines": _spec({}, _LINE + " unitPrice shipmentDate", line=True),
    # Field names from the BC API v2.0 salesCreditMemo / salesCreditMemoLine resources
    # (MicrosoftDocs/dynamics365smb-devitpro-pb, api-reference/v2.0
    # ``dynamics_salescreditmemo.md``, ``dynamics_salescreditmemoline.md``); invoiceId /
    # invoiceNumber name the posted invoice the memo is applied to, blank when BC states
    # none. UNVERIFIED live until bc.capture_full captures them.
    "salesCreditMemos": _spec({"number": "number", "externalDocumentNumber": "number",
                               "invoiceNumber": "number"},
        "number externalDocumentNumber creditMemoDate postingDate dueDate customerId "
        "customerNumber customerName invoiceId invoiceNumber " + _HEADER_MONEY,
        orderby="postingDate creditMemoDate number"),
    "salesCreditMemoLines": _spec({}, _LINE + " unitPrice shipmentDate", line=True),
    # BC API v2.0 purchaseCreditMemo / purchaseCreditMemoLine (same docs tree,
    # ``dynamics_purchasecreditmemo.md``): invoiceId / invoiceNumber name the posted
    # purchase invoice the memo is applied to. UNVERIFIED live until bc.capture_full
    # records the units complete.
    "purchaseCreditMemos": _spec({"number": "number", "invoiceNumber": "number"},
        "number creditMemoDate postingDate dueDate vendorId vendorNumber vendorName "
        "invoiceId invoiceNumber " + _HEADER_MONEY,
        orderby="postingDate creditMemoDate number"),
    "purchaseCreditMemoLines": _spec({}, _LINE + " unitCost", line=True),
    # Ledgers, aged balances and payment journals (BC API v2.0 itemLedgerEntry,
    # generalLedgerEntry, agedAccountsReceivable/-Payable, customer/vendorPaymentJournal,
    # customer/vendorPayment): the fields the ontology bindings read
    # (ontology/schema/domains/bc_finance.json). The capture reads every field; the
    # allowlist is what ``probe --finance-entities`` checks BC accepts as ``$select``
    # (live check ``bc.ledgers``). UNVERIFIED live until that check passes.
    "itemLedgerEntries": _spec({"documentNumber": "number", "itemNumber": "number"},
        "entryNumber itemNumber postingDate entryType sourceNumber sourceType documentNumber "
        "documentType description quantity salesAmountActual costAmountActual"),
    "generalLedgerEntries": _spec({"documentNumber": "number", "accountNumber": "number"},
        "entryNumber postingDate documentNumber documentType accountId accountNumber "
        "description debitAmount creditAmount"),
    "agedAccountsReceivables": _spec({}, "customerNumber name currencyCode balanceDue "
        "currentAmount period1Amount period2Amount period3Amount agedAsOfDate "
        "periodLengthFilter", key="customerId"),
    "agedAccountsPayables": _spec({}, "vendorNumber name currencyCode balanceDue "
        "currentAmount period1Amount period2Amount period3Amount agedAsOfDate "
        "periodLengthFilter", key="vendorId"),
    "customerPaymentJournals": _spec({}, "code displayName"),
    "vendorPaymentJournals": _spec({}, "code displayName"),
    "customerPayments": BcQuerySpec({"id": "guid"}, frozenset((
        "id journalId journalDisplayName lineNumber customerId customerNumber postingDate "
        "documentNumber externalDocumentNumber amount appliesToInvoiceId "
        "appliesToInvoiceNumber description lastModifiedDateTime").split()),
        frozenset({"id", "journalId"})),
    "vendorPayments": BcQuerySpec({"id": "guid"}, frozenset((
        "id journalId journalDisplayName lineNumber vendorId vendorNumber postingDate "
        "documentNumber externalDocumentNumber amount appliesToInvoiceId "
        "appliesToInvoiceNumber description lastModifiedDateTime").split()),
        frozenset({"id", "journalId"})),
    "items": _spec({"number": "number"},
        "number displayName type itemCategoryCode blocked inventory unitPrice unitCost "
        "baseUnitOfMeasureCode"),
    "vendors": _spec({"number": "number"},
        "number displayName currencyCode paymentTermsId blocked balance"),
    "customers": _spec({"number": "number"},
        "number displayName type currencyCode paymentTermsId shipmentMethodId blocked "
        "balanceDue creditLimit"),
}


# Modified-since reads. The one
# non-equality filter BC reads may carry is ``lastModifiedDateTime ge <instant>``, and
# only on the entity sets below: BC API v2.0 documents a read-only
# ``lastModifiedDateTime`` on document headers, parties, items and the item/general
# ledger entries (MicrosoftDocs/dynamics365smb-devitpro-pb, api-reference/v2.0
# ``dynamics_*.md``). Line sets carry none and are never filtered by it. The instant is
# fixed-width UTC with milliseconds (``YYYY-MM-DDTHH:MM:SS.fffZ``), parsed as a real
# calendar time, and written into the filter by this module only. The same sets may
# be listed key-only (``$select=id,lastModifiedDateTime``) for the full pass that
# checks which keys still exist. UNVERIFIED live: BC's acceptance of an unquoted
# ``Edm.DateTimeOffset`` literal here (the documented OData v4 form) and whether its
# nextLink keeps the filter; ``bc.capture_incremental`` checks both.
BC_MODIFIED_FIELD = "lastModifiedDateTime"
BC_MODIFIED_SINCE_SETS = frozenset({
    "purchaseOrders", "purchaseReceipts", "purchaseInvoices", "purchaseCreditMemos",
    "salesQuotes", "salesOrders", "salesInvoices", "salesShipments", "salesCreditMemos",
    "customers", "vendors", "contacts", "items", "itemLedgerEntries",
    "generalLedgerEntries"})
BC_KEY_SELECT = "id," + BC_MODIFIED_FIELD
# Posted-only reads: the capture asks BC for ``status ne 'Draft'`` on
# these sets, so an unposted document is never read, stored or costed as posted. BC API v2.0
# salesInvoice ``status`` is Draft / In Review / Open / Paid / Canceled / Corrective.
# UNVERIFIED live: BC's acceptance of the filter on salesInvoices (bc.capture_full checks).
BC_POSTED_ONLY_SETS = frozenset({"salesInvoices"})
BC_POSTED_FILTER = "status ne 'Draft'"
# Calculated values per entity set: BC FlowFields and API fields computed from other
# tables (header totals and status from lines and ledger entries, balances, item
# inventory, ledger entry cost/sales amounts). They change without moving the
# record's etag or ``lastModifiedDateTime``, so neither a modified-since read nor a
# key-only confirmation can see the change: a set that declares any stays on full
# content reads (``integrations.bc_read.BcCapture`` auto mode). Every
# ``BC_MODIFIED_SINCE_SETS`` entry is declared here (an empty set means "none known");
# an undeclared set counts as calculated. Names on sets with a ``BC_QUERY_SPECS``
# entry are names of that spec's ``$select`` allowlist; names on other sets follow the
# BC API v2.0 resource reference (UNVERIFIED live, like the allowlists).
_HEADER_CALCULATED = ("discountAmount totalAmountExcludingTax totalTaxAmount "
                      "totalAmountIncludingTax status")
_CALCULATED_DECLARED = {
    "purchaseOrders": _HEADER_CALCULATED + " fullyReceived",
    "salesOrders": _HEADER_CALCULATED + " fullyShipped",
    "purchaseInvoices": _HEADER_CALCULATED, "salesInvoices": _HEADER_CALCULATED,
    "purchaseCreditMemos": _HEADER_CALCULATED, "salesCreditMemos": _HEADER_CALCULATED,
    "salesQuotes": _HEADER_CALCULATED,
    "customers": "balanceDue", "vendors": "balance", "items": "inventory",
    "itemLedgerEntries": "costAmountActual salesAmountActual",
    "purchaseReceipts": "", "salesShipments": "", "contacts": "",
    "generalLedgerEntries": "",
}
BC_CALCULATED_FIELDS: dict[str, frozenset[str]] = {
    name: frozenset(fields.split()) for name, fields in _CALCULATED_DECLARED.items()}


def bc_has_calculated_fields(entity_set: str) -> bool:
    """Whether a set holds (or may hold: undeclared) values that change without its
    etag or ``lastModifiedDateTime`` moving."""
    return entity_set not in BC_CALCULATED_FIELDS or bool(BC_CALCULATED_FIELDS[entity_set])
_MODIFIED_PREFIX = BC_MODIFIED_FIELD + " ge "
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
BC_INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def bc_instant_ok(value: Any) -> bool:
    """A fixed-width UTC instant ``YYYY-MM-DDTHH:MM:SS.fffZ`` naming a real time."""
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, BC_INSTANT_FORMAT)
    except ValueError:
        return False
    return True


def bc_query(entity_set: str, *, equals: tuple[str, str] | None = None,
             top: int | None = None, select: Any = None,
             orderby: str | None = None, modified_since: str | None = None,
             keys_only: bool = False, expand: str | None = None,
             posted_only: bool = False) -> dict[str, str]:
    """The query mapping for one BC read; ValueError on anything outside the spec.
    ``orderby`` is ``"<field> asc"`` or ``"<field> desc"`` with the field in the entity
    set's ``orderby`` allowlist. ``modified_since`` (a ``bc_instant_ok`` instant) gives
    ``$filter=lastModifiedDateTime ge <instant>`` and ``keys_only`` gives
    ``$select=id,lastModifiedDateTime``, both only on ``BC_MODIFIED_SINCE_SETS``;
    ``modified_since`` excludes ``equals`` and ``keys_only`` excludes ``select``.
    ``expand`` is the header set's own line set (``BC_DOCUMENT_LINES``), giving
    ``$expand=<line set>``; it excludes ``select`` and ``keys_only``.
    ``posted_only`` (``BC_POSTED_ONLY_SETS`` only) adds ``status ne 'Draft'`` to
    ``$filter`` (before the modified-since term, joined by ``and``); it excludes ``equals``."""
    if not TYPES["entity_set"].valid(entity_set):
        raise ValueError("entity set not in the read allowlist")
    spec = BC_QUERY_SPECS.get(entity_set)
    query: dict[str, str] = {}
    if not isinstance(keys_only, bool):
        raise ValueError("keys_only must be a bool")
    if not isinstance(posted_only, bool):
        raise ValueError("posted_only must be a bool")
    if posted_only and (entity_set not in BC_POSTED_ONLY_SETS or equals is not None):
        raise ValueError("posted_only not allowed for this read")
    if modified_since is not None or keys_only:
        if entity_set not in BC_MODIFIED_SINCE_SETS:
            raise ValueError("entity set has no lastModifiedDateTime")
    if modified_since is not None:
        if equals is not None:
            raise ValueError("one bc filter per read")
        if not bc_instant_ok(modified_since):
            raise ValueError("modified_since must be YYYY-MM-DDTHH:MM:SS.fffZ")
        query["$filter"] = _MODIFIED_PREFIX + modified_since
    if posted_only:
        query["$filter"] = (BC_POSTED_FILTER + " and " + query["$filter"]
                            if "$filter" in query else BC_POSTED_FILTER)
    if keys_only:
        if select is not None:
            raise ValueError("keys_only excludes select")
        query["$select"] = BC_KEY_SELECT
    if equals is not None:
        if spec is None or not isinstance(equals, tuple) or len(equals) != 2:
            raise ValueError("bc filter not allowed for this entity set")
        field, value = equals
        kind = spec.filters.get(field) if isinstance(field, str) else None
        if kind == "guid":
            if not _rx(_GUID)(value):
                raise ValueError(f"bc filter {field} needs a GUID")
            query["$filter"] = f"{field} eq {value.lower()}"
        elif kind == "number":
            if not bc_number_ok(value):
                raise ValueError(f"bc filter {field} value invalid")
            query["$filter"] = f"{field} eq '{value}'"
        else:
            raise ValueError("bc filter field not allowed")
    if top is not None:
        if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= BC_MAX_TOP:
            raise ValueError(f"$top must be 1..{BC_MAX_TOP}")
        query["$top"] = str(top)
    if select is not None:
        if isinstance(select, (str, bytes)):
            raise ValueError("select must be a list of field names")
        names = set(select)
        if (spec is None or not names
                or any(not isinstance(n, str) or n not in spec.select for n in names)):
            raise ValueError("select names not allowlisted")
        query["$select"] = ",".join(sorted(names | spec.always))
    if orderby is not None:
        found = orderby.split(" ") if isinstance(orderby, str) else []
        if (spec is None or len(found) != 2 or not _ids.valid("bc_field_name", found[0])
                or found[1] not in ("asc", "desc") or found[0] not in spec.orderby):
            raise ValueError("bc orderby not allowed for this entity set")
        query["$orderby"] = orderby
    if expand is not None:
        if expand != BC_DOCUMENT_LINES.get(entity_set) or select is not None or keys_only:
            raise ValueError("bc expand not allowed for this entity set")
        query["$expand"] = expand
    return query


# Parameters BC adds to an ``@odata.nextLink`` on top of the original query.
# UNVERIFIED live: taken from BC's documented server-driven paging
# (``$skiptoken``) and the ``aid`` (application id) BC is known to append;
# the live probe follows a nextLink only when a listing has more than one page.
_BC_CONTINUATION = frozenset({"$skiptoken", "aid"})
_ASCII_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_SKIPTOKEN_CHARS = _ASCII_LETTERS | frozenset("0123456789_=,{}:'-")
_GUID_CHARS = frozenset("0123456789abcdefABCDEF-")


def _continuation_values_ok(given: Mapping[str, str]) -> bool:
    token, aid = given.get("$skiptoken"), given.get("aid")
    if token is not None and not (1 <= len(token) <= 512 and all(
            c in _SKIPTOKEN_CHARS for c in token)):
        return False
    if aid is not None and not (1 <= len(aid) <= 16 and all(c in _ASCII_LETTERS for c in aid)):
        return False
    return True


def bc_query_ok(entity_set: str, query: str) -> bool:
    """Whether a raw query string is exactly one ``bc_query`` could have built
    (for a guard-side check of a BC GET's query)."""
    if not isinstance(query, str):
        return False
    if not query:
        return True
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    given = dict(pairs)
    sent = given
    continuation = _BC_CONTINUATION
    if len(given) != len(pairs) or (set(given) - {"$filter", "$top", "$select", "$orderby",
                                                  "$expand"} - continuation):
        return False
    equals = since = None
    posted_only = False
    text = given.get("$filter")
    if text is not None and text.startswith(BC_POSTED_FILTER):
        posted_only, text = True, text[len(BC_POSTED_FILTER):]
        if text.startswith(" and "):
            text = text[len(" and "):]
            if not text.startswith(_MODIFIED_PREFIX):
                return False
        elif text:
            return False
        given = {k: v for k, v in given.items() if k != "$filter"}
        if text:
            given["$filter"] = text
    if "$filter" in given and given["$filter"].startswith(_MODIFIED_PREFIX):
        since = given["$filter"][len(_MODIFIED_PREFIX):]  # validated by the rebuild
    elif "$filter" in given:
        name, eq, operand = given["$filter"].partition(" eq ")
        if not eq or not _ids.valid("bc_field_name", name):
            return False
        if len(operand) >= 2 and operand[0] == operand[-1] == "'" and "'" not in operand[1:-1]:
            equals = (name, operand[1:-1])
        elif len(operand) == 36 and all(c in _GUID_CHARS for c in operand):
            equals = (name, operand)
        else:
            return False
    top = None
    if "$top" in given:
        text = given["$top"]
        if not (1 <= len(text) <= 4 and text.isascii() and text.isdigit() and text[0] != "0"):
            return False
        top = int(given["$top"])
    keys_only = (given.get("$select") == BC_KEY_SELECT
                 and entity_set in BC_MODIFIED_SINCE_SETS)
    select = (given["$select"].split(",") if "$select" in given and not keys_only
              else None)
    try:
        rebuilt = bc_query(entity_set, equals=equals, top=top, select=select,
                           orderby=given.get("$orderby"), modified_since=since,
                           keys_only=keys_only, expand=given.get("$expand"),
                           posted_only=posted_only)
    except ValueError:
        return False
    if not _continuation_values_ok(given):
        return False
    return (all(rebuilt.get(k) == sent[k] for k in rebuilt)
            and set(sent) - continuation == set(rebuilt))


# ``bc.odata_purchase_orders`` (ODataV4 Page 50 ``PurchaseOrder``): exactly these three
# fields, always (``No`` is the document number, ``Your_Reference`` the FileMaker PO
# number, ``Buy_from_Vendor_No`` the vendor the capture cross-checks); at most one
# ``No eq '<number>'`` equality built from a validated number; a bounded ``$top``; the
# nextLink continuation. A web-service page's ``$skiptoken`` carries the page key (the
# document number), so for this operation only it may also hold the number characters
# ``bc_number_literal`` allows (space, ``.``, ``/``, ``#``). UNVERIFIED live: the
# skiptoken shape; the field names are the lead's live probe of 2026-10-21.
BC_ODATA_PO_SELECT = "No,Your_Reference,Buy_from_Vendor_No"
BC_ODATA_PO_FIELDS = tuple(BC_ODATA_PO_SELECT.split(","))
_ODATA_SKIPTOKEN_CHARS = _SKIPTOKEN_CHARS | frozenset(" ./#")


def bc_odata_purchase_order_query(*, number: str | None = None,
                                  top: int | None = None) -> dict[str, str]:
    """The query of one ``bc.odata_purchase_orders`` read; ValueError on a bad value."""
    query = {"$select": BC_ODATA_PO_SELECT}
    if number is not None:
        if not bc_number_ok(number):
            raise ValueError("bc odata filter value invalid")
        query["$filter"] = f"No eq '{number}'"
    if top is not None:
        if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= BC_MAX_TOP:
            raise ValueError(f"$top must be 1..{BC_MAX_TOP}")
        query["$top"] = str(top)
    return query


def bc_odata_purchase_order_query_ok(query: str) -> bool:
    """Whether a raw query is exactly one ``bc_odata_purchase_order_query`` builds, plus
    the continuation parameters (guard side, incl. nextLinks)."""
    if not isinstance(query, str) or not query:
        return False
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    given = dict(pairs)
    if len(given) != len(pairs) or set(given) - {"$select", "$filter", "$top"} - _BC_CONTINUATION:
        return False
    number = None
    if "$filter" in given:
        text = given["$filter"]
        if not (text.startswith("No eq '") and text.endswith("'") and len(text) > 8):
            return False
        number = text[len("No eq '"):-1]
    top = None
    if "$top" in given:
        text = given["$top"]
        if not (1 <= len(text) <= 4 and text.isascii() and text.isdigit() and text[0] != "0"):
            return False
        top = int(text)
    try:
        rebuilt = bc_odata_purchase_order_query(number=number, top=top)
    except ValueError:
        return False
    token, aid = given.get("$skiptoken"), given.get("aid")
    if token is not None and not (1 <= len(token) <= 512 and all(
            c in _ODATA_SKIPTOKEN_CHARS for c in token)):
        return False
    if aid is not None and not _continuation_values_ok({"aid": aid}):
        return False
    return (set(given) - _BC_CONTINUATION == set(rebuilt)
            and all(given[k] == v for k, v in rebuilt.items()))


def bc_request_query_ok(path: str, query: str) -> bool:
    """Guard-side check of one BC GET: the (encoded) path's operation decides
    which query it may carry. ``bc.entities`` / ``bc.lines.*``: exactly what
    ``bc_query`` builds for that entity set (plus the nextLink continuation
    parameters); ``bc.companies``: continuation parameters only; ``bc.entity``
    (get by id) and ``bc.metadata``: no query at all; ``bc.odata_purchase_orders``: exactly
    what ``bc_odata_purchase_order_query`` builds (its ``$select`` is required)."""
    op = match(BC, path)
    if op is None or not isinstance(query, str):
        return False
    if op in ("bc.entity", "bc.metadata"):
        return query == ""
    if op == "bc.companies":
        return _continuation_only(query)
    if op == "bc.entities":
        found = _COMPILED[op][1].fullmatch(path)
        return found is not None and bc_query_ok(unquote(found.group("entity")), query)
    if op.startswith("bc.lines."):
        return bc_query_ok(BC_DOCUMENT_LINES[op[len("bc.lines."):]], query)
    if op == "bc.odata_purchase_orders":
        return bc_odata_purchase_order_query_ok(query)
    return False


def _continuation_only(query: str) -> bool:
    if not query:
        return True
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    given = dict(pairs)
    return (len(given) == len(pairs) and set(given) <= _BC_CONTINUATION
            and _continuation_values_ok(given))


# ------------------------------------------------------------------ external queries
#
# Closed query grammar of the external hosts: per operation the parameters it may carry,
# each at most once, each value checked on its decoded form; a required one must be
# present. Freightcom operations carry no query. An unknown parameter, a repeated one or
# a malformed value refuses the request before I/O.

_SP_MARKETPLACE = _ids.system_pattern("amazon_marketplace_id")
_SP_SKU = _ids.system_pattern("amazon_seller_sku")
_SP_SHIPMENT = _ids.system_pattern("amazon_shipment_id")
# Instants are the one UTC second form the reader sends; a pagination token is opaque
# text over the base64/url-safe alphabet (formats, not identifier grammars: no regex).
SP_INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SP_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_.-")
SP_MAX_TOKEN = 4096
SP_SHIPMENT_STATUSES = frozenset({"WORKING", "READY_TO_SHIP", "SHIPPED", "RECEIVING",
                                  "CANCELLED", "DELETED", "CLOSED", "ERROR", "IN_TRANSIT",
                                  "DELIVERED", "CHECKED_IN"})
SP_MAX_LIST = 50
# getLabels: the label document kinds Fulfillment Inbound v0 defines, and a cap on the
# package/pallet counts and paging numbers it takes. Carton-id lists are not admitted.
SP_LABEL_PAGE_TYPES = frozenset({
    "PackageLabel_Letter_2", "PackageLabel_Letter_4", "PackageLabel_Letter_6",
    "PackageLabel_Letter_6_CarrierLeft", "PackageLabel_A4_2", "PackageLabel_A4_4",
    "PackageLabel_Plain_Paper", "PackageLabel_Plain_Paper_CarrierBottom",
    "PackageLabel_Thermal", "PackageLabel_Thermal_Unified", "PackageLabel_Thermal_NonPCP",
    "PackageLabel_Thermal_No_Carrier_Rotation"})
SP_LABEL_TYPES = frozenset({"BARCODE_2D", "UNIQUE", "PALLET"})
SP_MAX_LABEL_COUNT = 999


def _sp_instant_ok(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, SP_INSTANT_FORMAT)
    except (TypeError, ValueError):
        return False
    return parsed.strftime(SP_INSTANT_FORMAT) == value


def _sp_token_ok(value: str) -> bool:
    return 0 < len(value) <= SP_MAX_TOKEN and set(value) <= _SP_TOKEN_CHARS


def _sp_count_ok(value: str, *, minimum: int = 1) -> bool:
    """A plain decimal count in [minimum, SP_MAX_LABEL_COUNT], no sign or leading zero."""
    return (value.isascii() and value.isdigit() and (value == "0" or value[0] != "0")
            and minimum <= int(value) <= SP_MAX_LABEL_COUNT)


def _sp_list_ok(value: str, item: Callable[[str], bool]) -> bool:
    items = value.split(",")
    return 0 < len(items) <= SP_MAX_LIST and len(set(items)) == len(items) and all(
        item(v) for v in items)


def _sp_interval_ok(value: str) -> bool:
    start, sep, end = value.partition("--")
    return bool(sep) and _sp_instant_ok(start) and _sp_instant_ok(end) and (
        datetime.fromisoformat(start.replace("Z", "+00:00"))
        < datetime.fromisoformat(end.replace("Z", "+00:00")))


def _one_of(*values: str) -> Callable[[str], bool]:
    return lambda v: v in values


_marketplace = _rx(_SP_MARKETPLACE)
# operation suffix -> (required parameters, {parameter: value check})
SP_QUERIES: dict[str, tuple[frozenset[str], dict[str, Callable[[str], bool]]]] = {
    "inbound_shipments": (frozenset({"MarketplaceId", "QueryType"}), {
        "MarketplaceId": _marketplace,
        "QueryType": _one_of("SHIPMENT", "DATE_RANGE", "NEXT_TOKEN"),
        "ShipmentStatusList": lambda v: _sp_list_ok(v, lambda s: s in SP_SHIPMENT_STATUSES),
        "ShipmentIdList": lambda v: _sp_list_ok(v, _rx(_SP_SHIPMENT)),
        "LastUpdatedAfter": _sp_instant_ok, "LastUpdatedBefore": _sp_instant_ok,
        "NextToken": _sp_token_ok}),
    "inbound_shipment_items": (frozenset({"MarketplaceId"}), {"MarketplaceId": _marketplace}),
    "inbound_labels": (frozenset({"MarketplaceId", "PageType", "LabelType"}), {
        "MarketplaceId": _marketplace,
        "PageType": lambda v: v in SP_LABEL_PAGE_TYPES,
        "LabelType": lambda v: v in SP_LABEL_TYPES,
        "NumberOfPackages": _sp_count_ok, "NumberOfPallets": _sp_count_ok,
        "PageSize": _sp_count_ok, "PageStartIndex": lambda v: _sp_count_ok(v, minimum=0)}),
    "inventory_summaries": (frozenset({"granularityType", "granularityId", "marketplaceIds"}), {
        "granularityType": _one_of("Marketplace"), "granularityId": _marketplace,
        "marketplaceIds": _marketplace, "details": _one_of("true", "false"),
        "sellerSkus": lambda v: _sp_list_ok(v, _rx(_SP_SKU)),
        "startDateTime": _sp_instant_ok, "nextToken": _sp_token_ok}),
    "order_metrics": (frozenset({"marketplaceIds", "interval", "granularity"}), {
        "marketplaceIds": lambda v: _sp_list_ok(v, _marketplace),
        "interval": _sp_interval_ok,
        "granularity": _one_of("Hour", "Day", "Week", "Month", "Year", "Total"),
        "sku": _rx(_SP_SKU), "fulfillmentNetwork": _one_of("MFN", "AFN"),
        "buyerType": _one_of("B2B", "B2C", "All")}),
}


def external_query_ok(op: str | None, query: str) -> bool:
    """Whether ``query`` is one the closed grammar of external operation ``op`` allows."""
    if not isinstance(op, str) or not isinstance(query, str):
        return False
    if op.startswith("freightcom."):
        return query == ""
    parts = op.split(".")
    if len(parts) != 3 or parts[0] != "sp_api" or parts[1] not in SP_API_HOSTS \
            or parts[2] not in SP_QUERIES:
        return False
    required, checks = SP_QUERIES[parts[2]]
    if not query:
        return not required
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    given = dict(pairs)
    if len(given) != len(pairs) or not required <= set(given) or set(given) - set(checks):
        return False
    if given.get("QueryType") == "NEXT_TOKEN" and "NextToken" not in given:
        return False
    return all(isinstance(v, str) and checks[k](v) for k, v in given.items())


__all__ = ["BC_ODATA_PO_FIELDS", "BC_ODATA_PO_SELECT", "BC_PINNED_COMPANY_NAMES",
           "bc_company_name_ok", "bc_odata_purchase_order_query",
           "bc_odata_purchase_order_query_ok", "BC_CALCULATED_FIELDS", "BC_DOCUMENT_LINES", "BC_ENTITY_SETS", "BC_LINE_PARENT_FIELD",
           "BC_RECORD_KEY_FIELD", "BC_INSTANT_FORMAT", "BC_KEY_SELECT",
           "BC_MAX_TOP", "BC_MODIFIED_FIELD", "BC_MODIFIED_SINCE_SETS", "BC_QUERY_SPECS",
           "DRIVE_DOWNLOAD_URL_KEY", "DRIVE_DOWNLOAD_URL_SELECT", "LIST_ITEM_FIELDS_QUERY",
           "TEAMS_THREAD_SUFFIXES", "TEMPLATES", "TYPES", "WELL_KNOWN_MAIL_FOLDERS", "bc_has_calculated_fields", "bc_instant_ok", "bc_number_ok",
           "bc_query",
           "bc_query_ok", "bc_request_query_ok", "column_name_ok",
           "drive_item_select", "drive_list_item_fields_url", "encode_share_id",
           "drive_list_item_fields_url_ok", "EXTERNAL_HOSTS", "FREIGHTCOM_TEST",
           "SP_API_HOSTS", "SP_QUERIES", "external_query_ok", "match", "search_text_ok", "segments_clean", "url"]
