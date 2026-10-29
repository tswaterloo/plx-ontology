"""Read-only Microsoft Graph mail and SharePoint readers with resolvable citations.

All I/O goes through ``live_source_guard.GuardedTransport`` (GET only, exact
host allowlist, action-path denylist, no redirects, byte caps, timeouts).
Each target is first resolved to its canonical server identity (metadata
only), then entitlements are checked for the calling ``Actor`` against a declared
``LiveSourceAudience`` before any source read of that resource; items whose
source ACL is required but unreadable or not granting the actor are withheld
and only counted. A refused resource, a withheld item and a nonexistent item
read the same (``not_visible``).

Citations (stable, versioned, resolvable later by re-reading the source):

* ``graph-mail://<mailbox-object-id>/<immutable-message-id>@<changeKey>`` — messages are
  always read with ``Prefer: IdType="ImmutableId"`` so the id survives folder
  moves; ``changeKey`` changes with every edit.
* ``sharepoint://<drive-id>/<item-id>@<eTag>`` and, once content was read,
  ``...#sha256=<hex>`` — the eTag covers content and metadata.

``resolve_citation`` re-reads the item for the reading actor and reports
``current`` or ``changed_since`` (with ``current_citation``); nothing is
cached, so an older version's content is not retained here.

Current SharePoint columns: ``SharePointReader.list_item_fields`` reads a
visible file's own listItem (``/drives/{d}/items/{i}/listItem?expand=fields``)
after the same audience/ACL decision, keeps only the caller's column
allowlist and returns a ``sharepoint-listitem-fields/v1`` snapshot pinned to
the drive eTag (consumed by ``operating.doc_metadata``).
``SharePointReader.library_columns`` reads a whole library's column definitions
and a newest-first list-item sample (which Portal-taxonomy columns exist, which are
empty, how often each is filled; counts only), for an actor covering the whole drive.
They are live-unverified until ``devloop/live-checks/graph.sharepoint_listitem.json``
and ``devloop/live-checks/graph.sharepoint_library_columns.json`` pass.

Mail delta paging: ``GraphMailReader.message_delta`` reads one folder's
``messages/delta`` stream page by page with no page cap (``$select``
projection, optional ``receivedDateTime ge`` window, immutable ids), validates
every server link and every resumed cursor against the folder's own delta
collection, and yields ``next_url`` / ``delta_url`` for the caller to store.
``integrations.capture_mail`` builds the versioned L1 mail capture on it
(``python3 -m integrations.graph_read mail-capture ...``; counts only).
Live-unverified until ``devloop/live-checks/graph.mail_capture.json`` passes.
``integrations.capture_sharepoint`` builds the versioned SharePoint capture on
``SharePointReader`` (``... sharepoint-capture ...``; counts only), live-unverified
until ``devloop/live-checks/capture.sharepoint.json`` passes; after a full walk it
follows the drive's change feed (``SharePointReader.drive_delta``). ``SharePointSites``
enumerates the sites and document libraries the app may read for its estate discovery.
``integrations.capture_breadth`` runs both over the whole capture scope
(``... capture-breadth ...``), live-unverified until
``devloop/live-checks/capture.breadth.json`` passes.
``integrations.capture_teams`` captures Teams channel and chat messages on the
same ``SourceApi`` (``... teams-capture ...``; counts and blocked parts only),
live-unverified until ``devloop/live-checks/graph.teams_capture.json`` passes.
``integrations.capture_sharepoint_lists`` captures the items of the SharePoint lists its
scope names (``... sharepoint-list-capture ...``; counts and blocked parts only),
live-unverified until ``devloop/live-checks/capture.sharepoint_list_items.json`` passes.

Probe (operator shape check for the credentialed auditor; prints counts, key
names and booleans only — no subjects, bodies, names or ids):
``python3 -m integrations.graph_read probe [--mailbox UPN] [--content]``; a users or mail
read Graph refuses for a missing application permission (``probe_block``) is a top-level
``blocked_upstream`` part (exit 1) beside the SharePoint parts, still measured;
``python3 -m integrations.graph_read listitem-probe --drive ID --folder PATH``
(column names and counts only, never values);
``python3 -m integrations.graph_read library-columns-probe [--registry FILE]``
(the ``SHAREPOINT_DRIVE_ID`` library's taxonomy column states and counts only).
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
import sys
from typing import Any, Callable, Container, Iterator, Mapping, Sequence
from urllib.parse import quote, unquote, urlencode, urlsplit

from ontology.core import Actor

from . import live_source_grammar as grammar
from . import people_exclusion
from .live_source_guard import (GRAPH_HOST, GRAPH_SCOPE, BoundedBackoff, ClientCredentials,
                                GuardedTransport, LiveSourceAudience, NotVisible,
                                ReadOnlyGuardError, Secret, SourceApi, SourceNotFound,
                                SourceUnavailable, TokenProvider, canonical_sha256,
                                completeness_with_withheld, failure_diagnostics,
                                is_transport_failure, jsonable,
                                nextlink_refused, parse_handle, q, sharepoint_host_from_env)


GRAPH = f"https://{GRAPH_HOST}/v1.0"
MAIL_SCHEME = "graph-mail"
DRIVE_SCHEME = "sharepoint"
_IMMUTABLE = {"Prefer": 'IdType="ImmutableId"'}
_MAIL_SELECT = ("id,changeKey,lastModifiedDateTime,receivedDateTime,sentDateTime,subject,"
                "from,sender,toRecipients,ccRecipients,conversationId,internetMessageId,"
                "hasAttachments,webLink,bodyPreview,parentFolderId,internetMessageHeaders")
_DRIVE_SELECT = ("id,name,eTag,cTag,size,webUrl,createdDateTime,lastModifiedDateTime,"
                 "file,folder,parentReference,lastModifiedBy")
_DELTA_SELECT = "id,name,eTag,parentReference,file,folder,root,deleted"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
MAX_BODY_CHARS = 20_000
# Delta paging (``GraphMailReader.message_delta``): page size is a Prefer hint,
# never a cap on the round; the round ends only at ``@odata.deltaLink``.
MAX_DELTA_PAGE_SIZE = 200
# Attachment listing (metadata only): a hard cap on the items one listing follows
# through @odata.nextLink, over at most ATTACHMENT_LIST_PAGES pages.
MAX_ATTACHMENTS_LISTED = 1000
ATTACHMENT_LIST_PAGES = 50
# The folders one mailbox listing follows (``GraphMailReader.list_folders``).
MAX_FOLDERS = 1000


def _addresses(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    found = []
    for entry in values:
        address = (entry.get("emailAddress") or {}).get("address") if isinstance(entry, Mapping) else None
        if isinstance(address, str) and address:
            found.append(address.lower())
    return found


def _authentication_results(headers: Any) -> list[str] | None:
    """The ``Authentication-Results`` header values of a message, in message order (the
    topmost is the receiving server's; ``ontology.mentions.dmarc_pass``), or None when
    Graph returned no header list (no proof either way)."""
    if not isinstance(headers, list):
        return None
    return [entry["value"] for entry in headers
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
            and entry["name"].strip().lower() == "authentication-results"
            and isinstance(entry.get("value"), str)]


def mail_citation(mailbox: str, message_id: str, change_key: str) -> str:
    return f"{MAIL_SCHEME}://{q(mailbox)}/{q(message_id)}@{q(change_key)}"


def drive_citation(drive_id: str, item_id: str, etag: str, content_sha256: str | None = None) -> str:
    handle = f"{DRIVE_SCHEME}://{q(drive_id)}/{q(item_id)}@{q(etag)}"
    return handle + (f"#sha256={content_sha256}" if content_sha256 else "")


_UPN = re.compile(r"^[a-z0-9._%+'-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Mailbox:
    """A mailbox resolved by the directory: canonical id plus every address."""
    object_id: str            # canonical form used in citations
    upn: str
    identities: tuple[str, ...]  # object id, UPN, mail, SMTP proxy addresses
    resources: tuple[str, ...]   # mail:<identity> for each


def _smtp_addresses(raw: Mapping[str, Any]) -> set[str]:
    found = set()
    mail = raw.get("mail")
    if isinstance(mail, str) and mail:
        found.add(mail.lower())
    for proxy in raw.get("proxyAddresses") or []:
        if isinstance(proxy, str) and proxy.lower().startswith("smtp:"):
            found.add(proxy[5:].lower())
    return found


class GraphMailReader:
    """Entitlement-checked reads of mailbox messages (metadata, optional text body).

    One guarded entry point (``_open``): the caller's mailbox (object id or
    UPN, any case) is resolved by a directory metadata GET
    (``id,userPrincipalName,mail,proxyAddresses``; no mail content) to its
    full identity set. The audience is evaluated over ``mail:<identity>`` for
    every form (object id, UPN, mail, every SMTP proxy address), most
    restrictive wins, so a deny written on any address applies. Messages are
    then read by object id only, and each returned message must carry a
    ``parentFolderId`` that resolves under that same user (else dropped and
    counted as ``provenance_dropped``). Citations use the object id (stable
    across UPN renames): ``graph-mail://<object-id>/<message-id>@<changeKey>``.
    The mailbox-owner ACL lists every identity of the set.
    """

    def __init__(self, api: SourceApi, audience: LiveSourceAudience, *,
                 people: "people_exclusion.PeopleExclusion | None" = None) -> None:
        self._api = api
        self._audience = audience
        # The people-exclusion guard (``build_graph`` attaches it to the api): an
        # excluded person's mailbox is refused before its lookup, mail they sent or
        # receive (To/Cc/Bcc) dropped before it is returned; without a usable guard
        # every read refuses.
        self.people = people if people is not None else people_exclusion.from_api(api)
        # Directory lookups shared inside a ``directory_cache()`` block only.
        self._directory: dict[str, Any] | None = None
        self._directory_depth = 0

    def __repr__(self) -> str:
        return "GraphMailReader()"

    @contextmanager
    def directory_cache(self) -> Iterator[None]:
        """Reuse each mailbox's directory lookup (identity set; no mail content)
        for the duration of one bounded run (e.g. building one case's mail
        evidence). The audience, owner ACL and every message read are still
        decided per actor and per call; only the directory answer is reused, so
        a rename or added proxy address inside the block is seen at its end.
        Outside a block nothing is cached."""
        if self._directory_depth == 0:
            self._directory = {}
        self._directory_depth += 1
        try:
            yield
        finally:
            self._directory_depth -= 1
            if self._directory_depth == 0:
                self._directory = None

    def prepare_people(self) -> None:
        """Make the people guard ready (its ID list, then one directory listing that
        names nobody); ``ExclusionUnavailable`` when it cannot be."""
        self.people.require()
        self.people.bind(self._api)

    def _excluded_party(self, raw: Mapping[str, Any]) -> str | None:
        """``sender`` when an excluded person sent the message, ``recipient`` when one is
        a To/Cc/Bcc recipient (the message is kept out of every mailbox's capture, not
        only theirs), else None."""
        senders = [raw.get(key) for key in ("from", "sender") if isinstance(raw.get(key), Mapping)]
        if self.people.excluded(_addresses(senders)):
            return "sender"
        recipients = [entry for key in ("toRecipients", "ccRecipients", "bccRecipients")
                      if isinstance(raw.get(key), list) for entry in raw[key]]
        return "recipient" if self.people.excluded(_addresses(recipients)) else None

    def _open(self, actor: Actor, mailbox: Any) -> Mailbox:
        if not self._audience.may_read_any(actor, "mail:"):
            raise NotVisible()
        if not isinstance(mailbox, str):
            raise NotVisible()
        # Before any request naming the mailbox: an excluded object ID is refused
        # outright, an address once the guard knows the excluded people's addresses.
        try:
            lookup = grammar.url("graph.user", {"$select": "id,userPrincipalName,mail,proxyAddresses"},
                                 user=mailbox)
        except ValueError:
            raise NotVisible() from None
        if people_exclusion.is_object_id(mailbox):
            self.people.refuse([mailbox], "mailbox")
        self.prepare_people()
        self.people.refuse([mailbox], "mailbox")
        cache, key = self._directory, lookup.lower()
        raw = cache.get(key) if cache is not None else None
        if raw is None:
            try:
                raw = self._api.get_json(lookup)
            except SourceNotFound:
                raise NotVisible() from None
            if cache is not None and isinstance(raw, Mapping):
                cache[key] = raw
        if not isinstance(raw, Mapping):
            raise NotVisible()
        object_id = str(raw.get("id") or "").lower()
        upn = str(raw.get("userPrincipalName") or "").lower()
        if not grammar.TYPES["user_id"].valid(object_id) or _UPN.fullmatch(upn) is None:
            raise NotVisible()
        identities = {object_id, upn} | _smtp_addresses(raw)
        # The directory must have resolved exactly the name the caller gave.
        if mailbox.lower() not in identities:
            raise NotVisible()
        self.people.refuse(identities, "mailbox")
        ordered = tuple(sorted(identities))
        box = Mailbox(object_id, upn, ordered, tuple(f"mail:{i}" for i in ordered))
        self._audience.require_all(actor, box.resources)
        return box

    def _owner_acl(self, box: Mailbox) -> dict[str, Any]:
        return {"status": "derived", "kind": "mailbox_owner", "identities": list(box.identities)}

    def mailbox_owner_acl(self, actor: Actor, mailbox: str) -> dict[str, Any]:
        """The derived mailbox-owner ACL, from the directory lookup only.

        One metadata GET (``id,userPrincipalName,mail,proxyAddresses``); no
        message, folder or body is read. Callers that intersect the owner ACL
        with their own identities (``CaseRuntime`` for an employee) check it
        before they read any message content. Not visible -> ``NotVisible``.
        """
        box = self._open(actor, mailbox)
        acl = self._owner_acl(box)
        if not self._audience.visible_all(actor, box.resources, acl):
            raise NotVisible()
        return acl

    def _gate(self, actor: Actor, mailbox: Any) -> Mailbox:
        """``_open`` plus the owner ACL: the check every message read starts with."""
        box = self._open(actor, mailbox)
        if not self._audience.visible_all(actor, box.resources, self._owner_acl(box)):
            raise NotVisible()
        return box

    def open_mailbox(self, actor: Actor, mailbox: str) -> dict[str, Any]:
        """The canonical identity of a mailbox the actor may read (directory
        metadata GET only, no message read): object id, UPN, every identity,
        the ``mail:<identity>`` audience keys and the derived owner ACL. A
        capture store records these so a later reader is re-gated on the same
        identity set without a source read. Not visible -> ``NotVisible``."""
        box = self._gate(actor, mailbox)
        return {"object_id": box.object_id, "upn": box.upn,
                "identities": list(box.identities), "resources": list(box.resources),
                "source_acl": self._owner_acl(box)}

    def resolve_folder(self, actor: Actor, mailbox: str, folder: str) -> str:
        """The id of a folder (well-known name or id) under a readable mailbox."""
        if not grammar.TYPES["mail_folder"].valid(folder):
            raise NotVisible()
        box = self._gate(actor, mailbox)
        folder_id = self._folder_id(box, folder, {})
        if folder_id is None:
            raise NotVisible()
        return folder_id

    @staticmethod
    def delta_url(mailbox_id: str, folder_id: str, received_since: str | None) -> str:
        """The first URL of a folder's message delta round (``$select`` projection,
        optional ``receivedDateTime ge`` window, the folder addressed by id)."""
        query = {"$select": _MAIL_SELECT}
        if received_since is not None:
            if not isinstance(received_since, str) or _ISO.fullmatch(received_since) is None:
                raise ValueError("received_since must be UTC ISO like 2026-01-01T00:00:00Z")
            query["$filter"] = f"receivedDateTime ge {received_since}"
        return grammar.url("graph.folder_messages_delta", query, user=mailbox_id,
                           folder=folder_id)

    def message_delta(self, actor: Actor, mailbox: str, folder: str, *,
                      cursor: str | None = None, received_since: str | None = None,
                      page_size: int = 50) -> Iterator[dict[str, Any]]:
        """One folder's message delta stream, page by page, with no page cap.

        Graph delta query (``/users/{id}/mailFolders/{folder-id}/messages/delta``)
        with the ``$select`` projection and an optional ``receivedDateTime ge``
        window. ``cursor`` resumes a round: a stored ``next_url`` (mid-round)
        or ``delta_url`` (the next incremental round). Every continuation the
        server returns, and every resumed cursor, must name this mailbox
        folder's delta collection (``SourceApi.continuation_url`` against the first
        URL, so ``received_since`` must be the round's own); the URL sent keeps
        the grammar-built path and takes only the server's query.

        Same gate as every message read (directory lookup, audience over every
        identity, owner ACL) before the first page; the folder is resolved to
        its id first, so the server's ``mailFolders('<id>')`` links compare
        with the request. Each yielded page: ``items`` (message records, as
        ``list_messages``), ``removed`` (``{id, reason}`` for ``@removed``
        entries), ``provenance_dropped`` (a message whose ``parentFolderId``
        is not this folder), and exactly one of ``next_url`` / ``delta_url``
        (the validated URL to store). Messages are read with immutable ids.
        Throttling: the ``SourceApi`` retry (``BoundedBackoff``, honours
        ``Retry-After`` up to its cap) applies per page; a page that still fails
        raises ``SourceUnavailable`` and the caller resumes from the last cursor
        it stored. A refused continuation (server link or stored cursor) raises
        ``nextlink_refused``: the caller drops its cursors, never resumes on them.
        """
        if (isinstance(page_size, bool) or not isinstance(page_size, int)
                or not 1 <= page_size <= MAX_DELTA_PAGE_SIZE):
            raise ValueError(f"page_size must be 1..{MAX_DELTA_PAGE_SIZE}")
        if not grammar.TYPES["mail_folder"].valid(folder):
            raise NotVisible()
        box = self._gate(actor, mailbox)
        folder_id = self._folder_id(box, folder, {})
        if folder_id is None:
            raise NotVisible()
        first = self.delta_url(box.object_id, folder_id, received_since)
        if cursor is None:
            url = first
        else:
            url = self._api.continuation_url(first, cursor)
            if url is None:
                raise nextlink_refused("graph nextLink escaped the collection (stored cursor)")
        headers = {"Prefer": f'IdType="ImmutableId", odata.maxpagesize={page_size}'}
        seen: set[str] = set()
        while True:
            if url in seen:
                raise SourceUnavailable("graph pagination loop")
            seen.add(url)
            try:
                page = self._api.get_json(url, headers=headers)
            except ReadOnlyGuardError:
                if url == first:
                    raise
                # Backstop: the guard refused a continuation the check above let
                # through. The same typed refusal, never an uncaught stall on a cursor.
                raise nextlink_refused("graph nextLink escaped the collection "
                                       "(refused by the guard)") from None
            if not isinstance(page, Mapping) or not isinstance(page.get("value"), list):
                raise SourceUnavailable("graph listing lacks value list")
            links = [key for key in ("@odata.nextLink", "@odata.deltaLink") if key in page]
            if len(links) != 1:
                raise SourceUnavailable("graph listing invalid: delta page needs one link")
            following = self._api.continuation_url(first, page[links[0]])
            if following is None:
                raise nextlink_refused("graph nextLink escaped the collection")
            items, removed, dropped, people_dropped = [], [], 0, 0
            for raw in page["value"]:
                if not isinstance(raw, Mapping):
                    raise SourceUnavailable("graph listing item invalid")
                if "@removed" in raw:
                    if not grammar.TYPES["mail_id"].valid(raw.get("id")):
                        raise SourceUnavailable("Graph message lacks a valid id")
                    reason = raw["@removed"].get("reason") \
                        if isinstance(raw["@removed"], Mapping) else None
                    removed.append({"id": raw["id"],
                                    "reason": reason if reason in ("deleted", "changed")
                                    else "unknown"})
                    continue
                if not self._belongs(box, raw, {}, folder_id):
                    dropped += 1
                    continue
                if self._excluded_party(raw):
                    people_dropped += 1   # never recorded, stored or indexed
                    continue
                items.append(self._record(box, raw))
            last = links[0] == "@odata.deltaLink"
            yield {"source": "graph-mail", "mailbox": box.object_id, "folder_id": folder_id,
                   "items": items, "removed": removed, "provenance_dropped": dropped,
                   "people_dropped": people_dropped,
                   "next_url": None if last else following,
                   "delta_url": following if last else None}
            if last:
                return
            url = following

    def list_folders(self, actor: Actor, mailbox: str, *, max_folders: int = MAX_FOLDERS
                     ) -> dict[str, Any]:
        """Every mail folder of a readable mailbox (top-level folders and, depth first,
        their child folders): ``folders`` (``{id, total, parent}``: Graph's
        ``totalItemCount``, None when absent) and ``complete`` (False when a listing was
        cut or ``max_folders`` reached: the caller never treats the list as whole).
        Folder ids only, no names or mail content. Same gate as every message read."""
        box = self._gate(actor, mailbox)
        query = {"$select": "id,totalItemCount,childFolderCount", "$top": "100"}
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        complete = True
        pending: list[tuple[str | None, str]] = [(None, grammar.url(
            "graph.mail_folders", query, user=box.object_id))]
        while pending:
            parent, url = pending.pop(0)
            try:
                raws, completeness = self._api.get_pages(url, max_pages=20, max_items=2000,
                                                         headers=_IMMUTABLE)
            except SourceNotFound:
                complete = False   # a folder gone between two listings
                continue
            complete = complete and bool(completeness.get("complete"))
            for raw in raws:
                fid = raw.get("id") if isinstance(raw, Mapping) else None
                if not grammar.TYPES["mail_id"].valid(fid) or fid.lower() in seen:
                    continue
                if len(found) >= max_folders:
                    complete = False
                    break
                seen.add(fid.lower())
                total = raw.get("totalItemCount")
                found.append({"id": fid, "parent": parent,
                              "total": total if isinstance(total, int)
                              and not isinstance(total, bool) else None})
                children = raw.get("childFolderCount")
                if not isinstance(children, int) or children > 0:
                    pending.append((fid, grammar.url("graph.mail_child_folders", query,
                                                     user=box.object_id, folder=fid)))
        return {"mailbox": box.object_id, "folders": found, "complete": complete}

    def message_pages(self, actor: Actor, mailbox: str, folder_id: str, *,
                      before: str, cursor: str | None = None, page_size: int = 50
                      ) -> Iterator[dict[str, Any]]:
        """A folder's messages received before ``before``, newest first, page by page
        (the history a windowed delta round never lists): ``$orderby receivedDateTime
        desc`` with ``receivedDateTime lt <before>``. ``cursor`` resumes from a stored
        ``next_url``, which must name this folder's message collection with the same
        query (``SourceApi.continuation_url``). Each page: ``items`` (records as
        ``message_delta``), ``provenance_dropped``, ``people_dropped`` (an excluded
        sender or recipient: never recorded) and ``next_url`` (None at the end). The
        same gate as every message read before the first page."""
        if (isinstance(page_size, bool) or not isinstance(page_size, int)
                or not 1 <= page_size <= MAX_DELTA_PAGE_SIZE):
            raise ValueError(f"page_size must be 1..{MAX_DELTA_PAGE_SIZE}")
        if not isinstance(before, str) or _ISO.fullmatch(before) is None:
            raise ValueError("before must be UTC ISO like 2026-01-01T00:00:00Z")
        if not grammar.TYPES["mail_id"].valid(folder_id):
            raise NotVisible()
        box = self._gate(actor, mailbox)
        first = grammar.url("graph.folder_messages", {
            "$select": _MAIL_SELECT, "$top": str(page_size),
            "$orderby": "receivedDateTime desc",
            "$filter": f"receivedDateTime lt {before}"}, user=box.object_id, folder=folder_id)
        if cursor is None:
            url = first
        else:
            url = self._api.continuation_url(first, cursor)
            if url is None:
                raise nextlink_refused("graph nextLink escaped the collection (stored cursor)")
        seen: set[str] = set()
        while True:
            if url in seen:
                raise SourceUnavailable("graph pagination loop")
            seen.add(url)
            try:
                page = self._api.get_json(url, headers=_IMMUTABLE)
            except SourceNotFound:
                raise NotVisible() from None
            except ReadOnlyGuardError:
                if url == first:
                    raise
                raise nextlink_refused("graph nextLink escaped the collection "
                                       "(refused by the guard)") from None
            if not isinstance(page, Mapping) or not isinstance(page.get("value"), list):
                raise SourceUnavailable("graph listing lacks value list")
            following = None
            if page.get("@odata.nextLink") is not None:
                following = self._api.continuation_url(first, page["@odata.nextLink"])
                if following is None:
                    raise nextlink_refused("graph nextLink escaped the collection")
            items, dropped, people_dropped = [], 0, 0
            for raw in page["value"]:
                if not isinstance(raw, Mapping):
                    raise SourceUnavailable("graph listing item invalid")
                if not self._belongs(box, raw, {}, folder_id):
                    dropped += 1
                    continue
                if self._excluded_party(raw):
                    people_dropped += 1   # never recorded, stored or indexed
                    continue
                items.append(self._record(box, raw))
            yield {"source": "graph-mail", "mailbox": box.object_id, "folder_id": folder_id,
                   "items": items, "removed": [], "provenance_dropped": dropped,
                   "people_dropped": people_dropped, "next_url": following}
            if following is None:
                return
            url = following

    def _folder_id(self, box: Mailbox, folder: str, cache: dict[str, str | None]) -> str | None:
        """Resolve a folder (well-known name or id) under this user; None if not there."""
        key = folder.lower()
        if key not in cache:
            try:
                raw = self._api.get_json(grammar.url("graph.mail_folder", {"$select": "id"},
                                                     user=box.object_id, folder=folder),
                                         headers=_IMMUTABLE)
            except (SourceNotFound, ValueError):
                raw = None
            fid = raw.get("id") if isinstance(raw, Mapping) else None
            cache[key] = fid if grammar.TYPES["mail_id"].valid(fid) else None
            if cache[key]:
                cache[cache[key].lower()] = cache[key]
        return cache[key]

    def _belongs(self, box: Mailbox, raw: Mapping[str, Any], cache: dict[str, str | None],
                 expected_folder: str | None = None) -> bool:
        parent = raw.get("parentFolderId")
        if not grammar.TYPES["mail_id"].valid(parent):
            return False
        if expected_folder is not None:
            return parent == expected_folder
        return self._folder_id(box, parent, cache) == parent

    def _record(self, box: Mailbox, raw: Mapping[str, Any], *, body: str | None = None,
                body_truncated: bool = False) -> dict[str, Any]:
        message_id = raw.get("id")
        change_key = raw.get("changeKey")
        if not grammar.TYPES["mail_id"].valid(message_id) or not isinstance(change_key, str) \
                or not change_key:
            raise SourceUnavailable("Graph message lacks a valid id or changeKey")
        sender = _addresses([raw.get("from")]) if isinstance(raw.get("from"), Mapping) else []
        record = {
            "source": "graph-mail", "kind": "message", "mailbox": box.object_id,
            "mailbox_upn": box.upn, "id": message_id, "id_type": "immutable",
            "change_key": change_key, "parent_folder_id": raw.get("parentFolderId"),
            "last_modified": raw.get("lastModifiedDateTime"),
            "received_at": raw.get("receivedDateTime"), "sent_at": raw.get("sentDateTime"),
            "subject": raw.get("subject"), "from": sender[0] if sender else None,
            "to": _addresses(raw.get("toRecipients")), "cc": _addresses(raw.get("ccRecipients")),
            "conversation_id": raw.get("conversationId"),
            "internet_message_id": raw.get("internetMessageId"),
            "has_attachments": raw.get("hasAttachments"), "original_url": raw.get("webLink"),
            "body_preview": raw.get("bodyPreview"), "source_acl": self._owner_acl(box),
            "authentication_results": _authentication_results(raw.get("internetMessageHeaders")),
        }
        if body is not None:
            record["body_text"] = body
            record["body_truncated"] = body_truncated
        record["content_sha256"] = canonical_sha256(record)
        record["citation"] = mail_citation(box.object_id, message_id, change_key)
        return jsonable(record)

    def list_messages(self, actor: Actor, mailbox: str, *, folder: str | None = None,
                      received_since: str | None = None, page_size: int = 25,
                      max_pages: int = 4, max_items: int = 100,
                      oldest_first: bool = False, search: str | None = None) -> dict[str, Any]:
        """Message metadata of one mailbox, or of one of its folders.

        Newest-first by default. ``oldest_first`` orders by ``receivedDateTime``
        ascending, so a capped incremental read (``integrations.ingestion``)
        has read every message older than the newest one it returned and can
        advance a received-time cursor without skipping the gap a newest-first
        cap would leave. ``search``: only the messages Graph's ``$search`` finds for
        that one term (``live_source_grammar.search_text_ok``, sent quoted), in
        Graph's relevance order (Graph takes no ``$orderby`` or ``$filter`` with it);
        the same mailbox gate, owner ACL and folder provenance apply."""
        if not isinstance(page_size, int) or not 1 <= page_size <= 50:
            raise ValueError("page_size must be 1..50")
        if not isinstance(oldest_first, bool):
            raise ValueError("oldest_first must be a boolean")
        query = {"$select": _MAIL_SELECT, "$top": str(page_size),
                 "$orderby": "receivedDateTime " + ("asc" if oldest_first else "desc")}
        if search is not None:
            if not grammar.search_text_ok(search) or received_since is not None or oldest_first:
                raise ValueError("search takes one search term, without a window or order")
            del query["$orderby"]
            query["$search"] = f'"{search}"'
        if received_since is not None:
            if not isinstance(received_since, str) or _ISO.fullmatch(received_since) is None:
                raise ValueError("received_since must be UTC ISO like 2026-01-01T00:00:00Z")
            query["$filter"] = f"receivedDateTime ge {received_since}"
        if folder is not None and not grammar.TYPES["mail_folder"].valid(folder):
            raise NotVisible()
        box = self._open(actor, mailbox)
        if not self._audience.visible_all(actor, box.resources, self._owner_acl(box)):
            raise NotVisible()
        cache: dict[str, str | None] = {}
        expected = None
        if folder is not None:
            expected = self._folder_id(box, folder, cache)
            if expected is None:
                raise NotVisible()
            first = grammar.url("graph.folder_messages", query, user=box.object_id,
                                folder=expected)
        else:
            first = grammar.url("graph.messages", query, user=box.object_id)
        raws, completeness = self._api.get_pages(first, max_pages=max_pages,
                                                 max_items=max_items, headers=_IMMUTABLE)
        records, dropped, people_dropped = [], 0, 0
        for raw in raws:
            if not self._belongs(box, raw, cache, expected):
                dropped += 1
                continue
            if self._excluded_party(raw):
                people_dropped += 1
                continue
            records.append(self._record(box, raw))
        result = completeness_with_withheld(completeness, dropped)
        result["provenance_dropped"] = dropped
        result["people_dropped"] = people_dropped
        return {"source": "graph-mail", "mailbox": box.object_id, "mailbox_upn": box.upn,
                "items": records, "completeness": result}

    def get_message(self, actor: Actor, mailbox: str, message_id: str, *,
                    include_body: bool = False, max_body_chars: int = MAX_BODY_CHARS,
                    expected_folder_id: str | None = None) -> dict[str, Any]:
        """One message (optionally with its text body, cut at ``max_body_chars``).

        ``expected_folder_id``: the message must sit in exactly this folder
        (a delta capture that already resolved the folder); without it the
        message's ``parentFolderId`` is resolved under this mailbox (one more
        GET). Either way a message outside the mailbox is ``NotVisible``."""
        if not grammar.TYPES["mail_id"].valid(message_id):
            raise NotVisible()
        if (isinstance(max_body_chars, bool) or not isinstance(max_body_chars, int)
                or max_body_chars < 0):
            raise ValueError("max_body_chars must be a non-negative integer")
        if expected_folder_id is not None and not grammar.TYPES["mail_id"].valid(
                expected_folder_id):
            raise NotVisible()
        # The owner ACL is known before any message read; check it first.
        box = self._gate(actor, mailbox)
        select = _MAIL_SELECT + (",body" if include_body else "")
        headers = dict(_IMMUTABLE)
        if include_body:
            headers["Prefer"] = 'IdType="ImmutableId", outlook.body-content-type="text"'
        try:
            raw = self._api.get_json(grammar.url("graph.message", {"$select": select},
                                                 user=box.object_id, message=message_id),
                                     headers=headers)
        except SourceNotFound:
            raise NotVisible() from None
        if not isinstance(raw, Mapping) or raw.get("id") != message_id:
            raise SourceUnavailable("Graph message read returned another identity")
        if not self._belongs(box, raw, {}, expected_folder_id):
            raise NotVisible()
        party = self._excluded_party(raw)
        if party:
            raise people_exclusion.PeopleExcluded(party)
        body, truncated = None, False
        if include_body:
            content = (raw.get("body") or {}).get("content") if isinstance(raw.get("body"), Mapping) else ""
            content = content if isinstance(content, str) else ""
            truncated = len(content) > max_body_chars
            body = content[:max_body_chars]
        return self._record(box, raw, body=body, body_truncated=truncated)

    def list_attachments(self, actor: Actor, mailbox: str, message_id: str, *,
                         max_items: int = 50) -> dict[str, Any]:
        """Attachment metadata of one message: id, name, content type, size, inline flag.

        Pages follow ``@odata.nextLink`` up to ``max_items`` (hard cap
        ``MAX_ATTACHMENTS_LISTED``) over at most 50 pages; a listing stopped by
        either cap is ``completeness.complete: false`` (never silently whole).

        ``$select`` excludes ``contentBytes``, so no attachment content is read.
        Same mailbox gate as ``get_message`` (directory lookup, audience over
        every identity, owner ACL before any read). Callers that need the
        message's provenance (folder under the mailbox) read it with
        ``get_message`` first. Names are untrusted text from the sender.
        """
        if not grammar.TYPES["mail_id"].valid(message_id):
            raise NotVisible()
        if isinstance(max_items, bool) or not isinstance(max_items, int) \
                or not 1 <= max_items <= MAX_ATTACHMENTS_LISTED:
            raise ValueError(f"max_items must be 1..{MAX_ATTACHMENTS_LISTED}")
        box = self._open(actor, mailbox)
        if not self._audience.visible_all(actor, box.resources, self._owner_acl(box)):
            raise NotVisible()
        url = grammar.url("graph.message_attachments",
                          {"$select": "id,name,contentType,size,isInline,lastModifiedDateTime"},
                          user=box.object_id, message=message_id)
        try:
            raws, completeness = self._api.get_pages(url, max_pages=ATTACHMENT_LIST_PAGES,
                                                     max_items=max_items, headers=_IMMUTABLE)
        except SourceNotFound:
            raise NotVisible() from None
        items = []
        for raw in raws:
            attachment_id, name = raw.get("id"), raw.get("name")
            if not isinstance(attachment_id, str) or not attachment_id:
                raise SourceUnavailable("Graph attachment lacks an id")
            size = raw.get("size")
            items.append(jsonable({
                "id": attachment_id, "name": name if isinstance(name, str) else None,
                "content_type": raw.get("contentType"),
                "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
                "is_inline": raw.get("isInline") is True,
                "kind": raw.get("@odata.type"),
                "last_modified": raw.get("lastModifiedDateTime")}))
        return {"source": "graph-mail", "mailbox": box.object_id, "message_id": message_id,
                "items": items, "completeness": completeness}

    def get_attachment(self, actor: Actor, mailbox: str, message_id: str,
                       attachment_id: str, *, max_bytes: int | None = None,
                       message: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One file attachment's bytes, after the same gate as ``get_message``.

        Order, before any attachment byte is requested: directory lookup and
        audience over every mailbox identity, the owner ACL, then the message's
        own metadata read (``get_message``: it must sit in a folder under this
        mailbox, else ``NotVisible``). Then one GET of
        ``/users/{id}/messages/{m}/attachments/{a}`` (the guard's one large-
        response operation, < 28 MB of base64 JSON), decoded and checked by
        ``graph_evidence.decode_file_attachment`` (file attachments only).
        Returns ``message`` (the message record), ``attachment`` (metadata with
        ``content_sha256``), ``content`` (bytes) and ``citation``
        (``graph-mail://...#attachment=<id>&sha256=<hex>``).

        ``message``: the record this reader returned for the same message in
        the same run (a capture reads the message with its body first). The
        mailbox gate still runs; only the repeated message GET is skipped, and
        the record must name this mailbox and message.
        """
        from .graph_evidence import (MAX_ATTACHMENT_BYTES, decode_file_attachment,
                                     mail_attachment_citation)

        if not grammar.TYPES["mail_id"].valid(attachment_id):
            raise NotVisible()
        cap = MAX_ATTACHMENT_BYTES if max_bytes is None else max_bytes
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_ATTACHMENT_BYTES:
            raise ValueError(f"max_bytes must be 1..{MAX_ATTACHMENT_BYTES}")
        if message is None:
            message = self.get_message(actor, mailbox, message_id)
        else:
            box = self._gate(actor, mailbox)
            if (not isinstance(message, Mapping) or message.get("mailbox") != box.object_id
                    or message.get("id") != message_id
                    or not isinstance(message.get("citation"), str)):
                raise ValueError("message record does not name this mailbox and message")
            message = {key: value for key, value in message.items()
                       if key not in ("body_text", "body_truncated")}
            message["citation"] = mail_citation(box.object_id, message_id,
                                                message["change_key"])
        try:
            raw = self._api.get_json(grammar.url("graph.message_attachment", None,
                                                 user=message["mailbox"], message=message_id,
                                                 attachment=attachment_id),
                                     headers=_IMMUTABLE,
                                     max_bytes=(cap * 4) // 3 + 64_000)
        except SourceNotFound:
            raise NotVisible() from None
        metadata, content = decode_file_attachment(raw, attachment_id, max_bytes=cap)
        return {"message": message, "attachment": jsonable(metadata), "content": content,
                "citation": mail_attachment_citation(message["citation"], attachment_id,
                                                     metadata["content_sha256"])}

    def resolve_citation(self, handle: str, actor: Actor) -> dict[str, Any]:
        (mailbox, message_id), change_key, _extra = parse_handle(handle, MAIL_SCHEME, 2)
        if not grammar.TYPES["user_id"].valid(mailbox) or mailbox != mailbox.lower():
            raise NotVisible()  # citations are minted with the object id only
        current = self.get_message(actor, mailbox, message_id)
        status = "current" if current["change_key"] == change_key else "changed_since"
        return {"citation": handle, "status": status, "cited_version": change_key,
                "current_version": current["change_key"],
                "current_citation": current["citation"], "record": current}


_ITEM_SELECT = _DRIVE_SELECT + ",root"


def quick_xor_hash(data: bytes) -> str:
    """Microsoft's QuickXorHash (base64), the hash OneDrive for Business and SharePoint
    publish for every file: byte ``i`` XORed into a 160-bit value at bit ``11*i mod 160``
    (rotating), then the length (64-bit little-endian) XORed into its last 8 bytes. The
    shift repeats every 160 bytes, so the data is folded into 160 bytes first."""
    folded = 0
    for start in range(0, len(data), 160):
        folded ^= int.from_bytes(data[start:start + 160], "little")
    value, mask = 0, (1 << 160) - 1
    for index, byte in enumerate(folded.to_bytes(160, "little")):
        shift = index * 11 % 160
        value ^= ((byte << shift) | (byte >> (160 - shift))) & mask
    digest = bytearray(value.to_bytes(20, "little"))
    for index, byte in enumerate(len(data).to_bytes(8, "little")):
        digest[12 + index] ^= byte
    return base64.b64encode(bytes(digest)).decode("ascii")


def undecided_not_visible(why: str) -> NotVisible:
    """A ``NotVisible`` the source did not decide: ``why`` is ``not_found`` (HTTP 404: the
    item is deleted or was never there) or ``acl_unreadable`` (its permission list was not
    read in full). Same type and message as every ``NotVisible``, so no answer changes;
    the ``undecided`` attribute tells an in-process caller it is no access refusal
    (``CaptureCaseHostTools._live_check``). Never part of a tool result."""
    exc = NotVisible()
    exc.undecided = why  # type: ignore[attr-defined]
    return exc


def permission_acl(perms: Sequence[Mapping[str, Any]], completeness: Mapping[str, Any]
                   ) -> dict[str, Any]:
    """A SharePoint permission listing (a drive item's or a list item's) as source
    identities, the ACL a capture keeps with the item.

    User grants contribute their email, login name and object id (lower case); groups,
    site groups and applications contribute ``<kind>:<id>`` (and ``<kind>:<email>``);
    sharing links contribute ``link-scope:<scope>``. Group membership is not expanded,
    so a group grant matches only a principal declared with that group identity."""
    identities: set[str] = set()
    grants = []
    for perm in perms:
        granted: set[str] = set()
        holders = [perm.get("grantedToV2")] + list(perm.get("grantedToIdentitiesV2") or [])
        for holder in holders:
            if not isinstance(holder, Mapping):
                continue
            for kind in ("user", "siteUser", "group", "siteGroup", "application"):
                entry = holder.get(kind)
                if not isinstance(entry, Mapping):
                    continue
                for key in ("email", "loginName", "id"):
                    value = entry.get(key)
                    if isinstance(value, str) and value:
                        prefix = "" if kind in ("user", "siteUser") else kind.lower() + ":"
                        granted.add(prefix + value.lower())
        link = perm.get("link")
        if isinstance(link, Mapping) and isinstance(link.get("scope"), str):
            granted.add("link-scope:" + link["scope"].lower())
        identities |= granted
        grants.append({"roles": sorted(r for r in perm.get("roles", []) if isinstance(r, str)),
                       "identities": sorted(granted),
                       "inherited": isinstance(perm.get("inheritedFrom"), Mapping)})
    # A truncated permission list cannot prove absence of a grant; "partial"
    # therefore never grants.
    status = "read" if completeness["complete"] else "partial"
    return {"status": status, "identities": sorted(identities), "grants": grants}


class SharePointReader:
    """Entitlement-checked reads of one SharePoint document library (drive).

    One guarded entry point per target: the caller's folder path or item id
    is resolved by a metadata-only GET (no content, no download URL) to the
    item Graph actually holds; its canonical path is derived from the
    server's ``parentReference.path`` + ``name``; the audience is checked on
    ``sharepoint:<drive>:<canonical path>`` only; children are then listed and
    content read by item id. Caller paths must be valid ``drive_path``
    grammar (no trailing space/dot, ``%``, ``:``, ``\\``, empty or dot
    segments), else ``not_visible``; an actor without any grant on the drive
    causes no source I/O at all.

    Audience keys are **drive-relative** server paths (``/Customers/Acme``),
    not site-relative (``/Shared Documents/...``) and without the library name.
    A folder answers to both its own key ``/X`` and its container key ``/X/``
    (most restrictive wins), so a deny on ``/X/*`` also hides the folder item
    ``X``. Every listed child must carry ``parentReference.id`` equal to the
    resolved folder id and this drive's ``driveId``, else it is dropped and
    counted (``provenance_dropped``).
    """

    def __init__(self, api: SourceApi, audience: LiveSourceAudience, drive_id: str, *,
                 transport: GuardedTransport, read_permissions: bool = False,
                 max_acl_reads: int = 50) -> None:
        self._api = api
        self._audience = audience
        if not grammar.TYPES["drive_id"].valid(drive_id):
            raise ValueError("drive id invalid")
        self.drive_id = drive_id
        self._transport = transport
        self._read_permissions = read_permissions
        if not isinstance(max_acl_reads, int) or not 0 <= max_acl_reads <= 5000:
            raise ValueError("max_acl_reads must be 0..5000")
        self._max_acl_reads = max_acl_reads
        # The child ``list_folder`` hands to its ``each`` right now (raw, canonical path,
        # ACL just read); None outside that call. See ``get_content(from_listing=...)``.
        self._in_hand: tuple[Mapping[str, Any], str, dict] | None = None

    def __repr__(self) -> str:
        return "SharePointReader()"

    def resource(self, path: str) -> str:
        return f"sharepoint:{self.drive_id}:{path}"

    def resources(self, path: str, is_folder: bool) -> list[str]:
        """Every key an item answers to: its path, plus ``<path>/`` for folders."""
        keys = [self.resource(path)]
        if is_folder and path != "/":
            keys.append(self.resource(path.rstrip("/") + "/"))
        return keys

    @staticmethod
    def _is_folder(raw: Mapping[str, Any]) -> bool:
        return isinstance(raw.get("folder"), Mapping) or isinstance(raw.get("root"), Mapping)

    def _gate(self, actor: Actor) -> None:
        if not self._audience.may_read_any(actor, f"sharepoint:{self.drive_id}:"):
            raise NotVisible()

    @staticmethod
    def _segments(path: Any, *, caller: bool) -> list[str]:
        if (not isinstance(path, str) or len(path) > 1024 or "\\" in path
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise NotVisible()
        if path in ("", "/"):
            return []
        inner = path[1:] if path.startswith("/") else path
        parts = inner.split("/")
        if caller and parts and parts[-1] == "":
            parts = parts[:-1]  # one trailing slash on a folder path is fine
        for part in parts:
            if part in ("", ".", "..") or part != part.strip():
                raise NotVisible()
            if caller and part.endswith("."):
                raise NotVisible()
        return parts

    def _source_path(self, raw: Mapping[str, Any]) -> str:
        """Canonical path from server metadata; malformed → NotVisible."""
        if isinstance(raw.get("root"), Mapping):
            return "/"
        parent = raw.get("parentReference") if isinstance(raw.get("parentReference"), Mapping) else {}
        if parent.get("driveId") != self.drive_id:
            raise NotVisible()
        parent_path, name = parent.get("path"), raw.get("name")
        if (not isinstance(parent_path, str) or "root:" not in parent_path
                or not isinstance(name, str) or not name or "/" in name):
            raise NotVisible()
        folder = unquote(parent_path.split("root:", 1)[1])
        parts = self._segments(folder, caller=False) + self._segments(name, caller=False)
        return "/" + "/".join(parts)

    def _permissions(self, item_id: str, *, required: bool) -> dict[str, Any]:
        """Item permissions as source identities (``permission_acl``); ``unreadable``
        denies where required."""
        if not (required or self._read_permissions):
            return {"status": "not_requested", "identities": []}
        url = grammar.url("graph.drive_permissions", drive=self.drive_id, item=item_id)
        try:
            perms, completeness = self._api.get_pages(url, max_pages=3, max_items=200)
        except SourceUnavailable:
            return {"status": "unreadable", "identities": []}
        return permission_acl(perms, completeness)

    def _record(self, raw: Mapping[str, Any], path: str, acl: Mapping[str, Any],
                content_sha256: str | None = None) -> dict[str, Any]:
        item_id, etag = raw.get("id"), raw.get("eTag")
        if not isinstance(item_id, str) or not item_id or not isinstance(etag, str) or not etag:
            raise SourceUnavailable("drive item lacks id or eTag")
        file_facet = raw.get("file") if isinstance(raw.get("file"), Mapping) else None
        modifier = ((raw.get("lastModifiedBy") or {}).get("user") or {}) \
            if isinstance(raw.get("lastModifiedBy"), Mapping) else {}
        record = {
            "source": "sharepoint-drive", "kind": "file" if file_facet else
            ("folder" if isinstance(raw.get("folder"), Mapping) else "item"),
            "drive_id": self.drive_id, "id": item_id, "etag": etag, "ctag": raw.get("cTag"),
            "path": path, "name": raw.get("name"), "size": raw.get("size"),
            "mime_type": file_facet.get("mimeType") if file_facet else None,
            "source_hashes": dict(file_facet.get("hashes") or {}) if file_facet else {},
            "created_at": raw.get("createdDateTime"),
            "last_modified": raw.get("lastModifiedDateTime"),
            "last_modified_by": modifier.get("email") if isinstance(modifier, Mapping) else None,
            "original_url": raw.get("webUrl"), "source_acl": dict(acl),
        }
        if content_sha256:
            record["content_sha256_bytes"] = content_sha256
        record["content_sha256"] = canonical_sha256(record)
        record["citation"] = drive_citation(self.drive_id, item_id, etag, content_sha256)
        return jsonable(record)

    def _metadata(self, item_id: str, *, download_url: bool = False,
                  sharepoint_ids: bool = False) -> Mapping[str, Any]:
        base = _ITEM_SELECT + (",sharepointIds" if sharepoint_ids else "")
        select = grammar.drive_item_select(base, download_url=download_url)
        url = grammar.url("graph.drive_item", {"$select": select}, drive=self.drive_id,
                          item=item_id)
        try:
            raw = self._api.get_json(url)
        except SourceNotFound:
            raise undecided_not_visible("not_found") from None
        if not isinstance(raw, Mapping) or raw.get("id") != item_id:
            raise SourceUnavailable("drive item read returned another identity")
        return raw

    def list_folder(self, actor: Actor, path: str = "/", *, page_size: int = 100,
                    max_pages: int = 5, max_items: int = 500,
                    stop: Callable[[], bool] | None = None,
                    skip: Container[str] = frozenset(),
                    each: Callable[[dict[str, Any]], bool | None] | None = None,
                    folder_id: str | None = None,
                    start: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Children of the folder Graph resolves for ``path`` (or, with ``folder_id``, of
        that folder item wherever it is now: a renamed or moved folder keeps its
        identity), audience-checked on that folder's server-derived canonical path,
        listed by its item id.

        ``stop`` (a caller's time budget) is asked before each item's permission read;
        once it answers true the listing ends there: the items not yet read are left
        out and counted (``completeness.budget_cut``, ``items_unread``), never returned
        without their permissions, and the listing is incomplete. ``skip``: ids of
        children the caller already holds from an earlier listing of this folder (a
        resumed walk): left out before any permission read and counted
        (``items_skipped``), never withheld. ``each`` receives every returned record as
        soon as its permissions are read, before the next child's, so the caller's work
        on it runs inside the ``stop`` budget; ``each`` answering False leaves that child
        unread as a budget cut does (the caller could not finish it).

        ``completeness.next`` is the listing's resume point, None once it read to the
        end: ``{"url", "offset"}``, the page holding the first child not handed back
        (the page and item caps, a budget cut) and its position on that page. Passed back
        as ``start`` (with ``folder_id`` the listed folder's id) a later listing,
        another run's too, continues there through the same audience and permission
        checks; a ``start`` that is not a continuation of this folder's listing raises
        ValueError before any page is read."""
        if not isinstance(page_size, int) or not 1 <= page_size <= 200:
            raise ValueError("page_size must be 1..200")
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 50:
            raise ValueError("max_pages must be 1..50")
        if not isinstance(max_items, int) or not 1 <= max_items <= 5000:
            raise ValueError("max_items must be 1..5000")
        if start is not None and (
                not isinstance(start, Mapping) or set(start) != {"url", "offset"}
                or not isinstance(start["url"], str) or type(start["offset"]) is not int
                or not 0 <= start["offset"] < 5000):
            raise ValueError("start must be a listing's next {url, offset}")
        self._gate(actor)
        if folder_id is not None:
            if not grammar.TYPES["item_id"].valid(folder_id):
                raise NotVisible()
            folder = self._metadata(folder_id)
        else:
            parts = self._segments(path, caller=True)
            select = {"$select": _ITEM_SELECT}
            try:
                url = (grammar.url("graph.drive_root", select, drive=self.drive_id) if not parts
                       else grammar.url("graph.drive_path", select, drive=self.drive_id,
                                        path="/" + "/".join(parts)))
            except ValueError:
                raise NotVisible() from None
            try:
                folder = self._api.get_json(url)
            except SourceNotFound:
                raise NotVisible() from None
        if (not isinstance(folder, Mapping) or not grammar.TYPES["item_id"].valid(folder.get("id"))
                or not self._is_folder(folder)):
            raise NotVisible()
        canonical = self._source_path(folder)
        container = canonical.rstrip("/") + "/"
        self._audience.require_all(actor, self.resources(canonical, True))
        first = grammar.url("graph.drive_children",
                            {"$select": _DRIVE_SELECT, "$top": str(page_size)},
                            drive=self.drive_id, item=folder["id"])
        raws, where, completeness = self._children_pages(first, start, max_pages, max_items)
        records, withheld, dropped, acl_reads, acl_capped = [], 0, 0, 0, False
        unread = skipped = 0
        for position, raw in enumerate(raws):
            parent = raw.get("parentReference") if isinstance(raw.get("parentReference"), Mapping) else {}
            if (parent.get("id") != folder["id"] or parent.get("driveId") != self.drive_id
                    or not grammar.TYPES["item_id"].valid(raw.get("id"))):
                dropped += 1
                continue
            try:
                if isinstance(parent.get("path"), str):
                    item_path = self._source_path(raw)
                    if item_path.rsplit("/", 1)[0] + "/" != container:
                        raise NotVisible()
                else:
                    name_parts = self._segments(raw.get("name"), caller=False)
                    if len(name_parts) != 1:
                        raise NotVisible()
                    item_path = container + name_parts[0]
            except NotVisible:
                dropped += 1
                continue
            if raw["id"] in skip:
                skipped += 1
                continue
            keys = self.resources(item_path, self._is_folder(raw))
            if not self._audience.allows_all(actor, keys):
                withheld += 1
                continue
            required = self._audience.source_acl_required_any(keys)
            if (required or self._read_permissions) and stop is not None and stop():
                unread = len(raws) - position
                completeness["next"] = where[position]
                break
            if (required or self._read_permissions) and acl_reads >= self._max_acl_reads:
                acl, acl_capped = {"status": "not_read_bound", "identities": []}, True
            else:
                if required or self._read_permissions:
                    acl_reads += 1
                acl = self._permissions(raw["id"], required=required)
            if not self._audience.visible_all(actor, keys, acl):
                withheld += 1
                continue
            records.append(self._record(raw, item_path, acl))
            if each is not None:
                self._in_hand = (raw, item_path, acl)
                try:
                    handled = each(records[-1])
                finally:
                    self._in_hand = None
                if handled is False:
                    records.pop()
                    unread = len(raws) - position
                    completeness["next"] = where[position]
                    break
        result = completeness_with_withheld(completeness, withheld + dropped)
        result["items_returned"] -= unread + skipped
        result["items_skipped"] = skipped
        result["provenance_dropped"] = dropped
        result["acl_reads"] = acl_reads
        result["acl_reads_capped"] = acl_capped
        result["budget_cut"] = bool(unread)
        result["items_unread"] = unread
        if unread:
            result["complete"] = False
        return {"source": "sharepoint-drive", "drive_id": self.drive_id, "path": canonical,
                "folder_id": folder["id"], "items": records, "completeness": result}

    def _children_pages(self, first: str, start: Mapping[str, Any] | None, max_pages: int,
                        max_items: int) -> tuple[list[Mapping[str, Any]],
                                                 list[dict[str, Any]], dict[str, Any]]:
        """The children pages of one listing from ``first`` (or its continuation
        ``start``) within the caps, each child's ``{"url", "offset"}``, and the
        ``get_pages`` completeness with ``next``: where a later listing resumes, None
        at the end. Every page is one ``get_page`` of the guarded client, whose nextLink
        check keeps it in the folder's collection; ``start["url"]`` passes the same
        check against ``first``. A failure keeps ``pages_read`` and ``items_read``."""
        url = first
        if start is not None:
            url = self._api.continuation_url(first, start["url"])
            if url is None:
                raise ValueError("start is not a continuation of this folder's listing")
        offset = 0 if start is None else start["offset"]
        raws: list[Mapping[str, Any]] = []
        where: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages, reason, resume = 0, None, None
        try:
            while True:
                if url in seen:
                    raise SourceUnavailable(f"{self._api.source} pagination loop")
                seen.add(url)
                items, following = self._api.get_page(url)
                pages += 1
                for index in range(offset, len(items)):
                    if len(raws) >= max_items:
                        reason, resume = "item_cap", {"url": url, "offset": index}
                        break
                    raws.append(items[index])
                    where.append({"url": url, "offset": index})
                offset = 0
                if reason is not None or following is None:
                    break
                if len(raws) >= max_items or pages >= max_pages:
                    reason = "item_cap" if len(raws) >= max_items else "page_cap"
                    resume = {"url": following, "offset": 0}
                    break
                url = following
        except SourceUnavailable as exc:
            exc.pages_read, exc.items_read = pages, len(raws)
            raise
        return raws, where, {"complete": reason is None, "truncated": reason is not None,
                             "truncation_reason": reason, "pages_read": pages,
                             "page_cap": max_pages, "item_cap": max_items,
                             "items_read": len(raws), "next": resume}

    def search(self, actor: Actor, query: str, *, max_items: int = 10) -> dict[str, Any]:
        """The files Graph's drive search finds for one term (``graph.drive_search``, one
        page of at most ``max_items``), each opened as ``get_item`` opens it (metadata GET,
        canonical path, audience and, where required, its ACL): a hit the actor may not
        read is withheld and counted; a folder hit, a hit of another drive or one that is
        not a file are dropped and counted (``provenance_dropped``). An actor without any
        grant on the drive causes no I/O; a term outside the grammar raises ValueError
        before any."""
        if not isinstance(max_items, int) or not 1 <= max_items <= 50:
            raise ValueError("max_items must be 1..50")
        self._gate(actor)
        url = grammar.url("graph.drive_search", {"$select": _DRIVE_SELECT,
                                                 "$top": str(max_items)},
                          drive=self.drive_id, q=query)
        raws, completeness = self._api.get_pages(url, max_pages=1, max_items=max_items)
        records, withheld, dropped = [], 0, 0
        for raw in raws:
            parent = raw.get("parentReference") if isinstance(raw.get("parentReference"),
                                                              Mapping) else {}
            if (self._is_folder(raw) or parent.get("driveId") not in (None, self.drive_id)
                    or not grammar.TYPES["item_id"].valid(raw.get("id"))):
                dropped += 1
                continue
            try:
                record = self.get_item(actor, raw["id"])
            except NotVisible:
                withheld += 1
                continue
            if record["kind"] != "file":
                dropped += 1
                continue
            records.append(record)
        result = completeness_with_withheld(completeness, withheld + dropped)
        result["provenance_dropped"] = dropped
        return {"source": "sharepoint-drive", "drive_id": self.drive_id, "items": records,
                "completeness": result}

    def drive_delta(self, actor: Actor, *, cursor: str | None = None
                    ) -> Iterator[dict[str, Any]]:
        """This drive's change feed (``/drives/{id}/root/delta``), page by page, no page cap.

        ``cursor`` None starts at the drive's current state (``token=latest``: no items,
        one deltaLink); otherwise a stored nextLink/deltaLink of this feed, which must
        name the same collection as the first URL (``SourceApi.continuation_url``), as must
        every link the server returns. ``Prefer: deltashowremovedasdeleted,
        deltashowsharingchanges`` asks Graph to list deleted items with a ``deleted``
        facet and items whose sharing changed.

        Only the drive gate runs here: a delta entry carries no path, so nothing in it is
        audience-checked. Entries are identity facts only (``id``, ``parent_id``,
        ``name``, ``kind`` file/folder/root, ``deleted``, ``etag``, ``shared_changed``)
        for the caller to decide which items to re-read through ``get_item`` (metadata,
        canonical path, audience, item permissions). An entry naming another drive is
        dropped and counted (``provenance_dropped``). Each page: ``items``,
        ``provenance_dropped`` and exactly one of ``next_url`` / ``delta_url``."""
        self._gate(actor)
        first = grammar.url("graph.drive_delta", {"$select": _DELTA_SELECT, "token": "latest"},
                            drive=self.drive_id)
        url = first if cursor is None else self._api.continuation_url(first, cursor)
        if url is None:
            raise SourceUnavailable("graph nextLink escaped the collection (stored cursor)")
        headers = {"Prefer": "deltashowremovedasdeleted, deltashowsharingchanges"}
        seen: set[str] = set()
        while True:
            if url in seen:
                raise SourceUnavailable("graph pagination loop")
            seen.add(url)
            page = self._api.get_json(url, headers=headers)
            if not isinstance(page, Mapping) or not isinstance(page.get("value"), list):
                raise SourceUnavailable("graph listing lacks value list")
            links = [key for key in ("@odata.nextLink", "@odata.deltaLink") if key in page]
            if len(links) != 1:
                raise SourceUnavailable("graph listing invalid: delta page needs one link")
            following = self._api.continuation_url(first, page[links[0]])
            if following is None:
                raise SourceUnavailable("graph nextLink escaped the collection")
            items, dropped = [], 0
            for raw in page["value"]:
                if not isinstance(raw, Mapping) or not grammar.TYPES["item_id"].valid(
                        raw.get("id")):
                    raise SourceUnavailable("graph delta item invalid")
                parent = raw.get("parentReference") \
                    if isinstance(raw.get("parentReference"), Mapping) else {}
                if parent.get("driveId") not in (None, self.drive_id):
                    dropped += 1
                    continue
                name = raw.get("name")
                items.append({
                    "id": raw["id"],
                    "parent_id": parent.get("id") if isinstance(parent.get("id"), str) else None,
                    "name": name if isinstance(name, str) else None,
                    "kind": ("root" if isinstance(raw.get("root"), Mapping)
                             else "folder" if isinstance(raw.get("folder"), Mapping)
                             else "file" if isinstance(raw.get("file"), Mapping) else "item"),
                    "deleted": isinstance(raw.get("deleted"), Mapping),
                    "etag": raw.get("eTag") if isinstance(raw.get("eTag"), str) else None,
                    "shared_changed": str(raw.get("@microsoft.graph.sharedChanged")
                                          ).lower() == "true"})
            last = links[0] == "@odata.deltaLink"
            yield {"items": items, "provenance_dropped": dropped,
                   "next_url": None if last else following,
                   "delta_url": following if last else None}
            if last:
                return
            url = following

    def library(self, actor: Actor) -> dict[str, Any]:
        """This drive as ``{id, name, drive_type}`` (``graph.drive``, one metadata GET
        after the drive gate); the name is the library's display name."""
        self._gate(actor)
        try:
            raw = self._api.get_json(grammar.url("graph.drive", {"$select": LIBRARY_SELECT},
                                                 drive=self.drive_id))
        except SourceNotFound:
            raise NotVisible() from None
        if not isinstance(raw, Mapping) or raw.get("id") != self.drive_id:
            raise SourceUnavailable("drive metadata identity mismatch")
        name, kind = raw.get("name"), raw.get("driveType")
        return {"id": self.drive_id, "name": name if isinstance(name, str) else None,
                "drive_type": kind if isinstance(kind, str) else None}

    def _open_item(self, actor: Actor, item_id: Any, *, sharepoint_ids: bool = False
                   ) -> tuple[Mapping[str, Any], str, dict]:
        """The guarded entry point for an item: metadata only, then audience + ACL."""
        self._gate(actor)
        if not grammar.TYPES["item_id"].valid(item_id):
            raise NotVisible()
        raw = self._metadata(item_id, sharepoint_ids=sharepoint_ids)
        path = self._source_path(raw)
        keys = self.resources(path, self._is_folder(raw))
        if not self._audience.allows_all(actor, keys):
            raise NotVisible()
        acl = self._permissions(item_id, required=self._audience.source_acl_required_any(keys))
        if not self._audience.visible_all(actor, keys, acl):
            # The audience allowed the path: a permission list not read in full (a
            # transient 429/5xx/timeout, a 404, a truncated list) denies undecided.
            raise (undecided_not_visible("acl_unreadable")
                   if acl.get("status") not in ("read", "derived") else NotVisible())
        return raw, path, acl

    def get_item(self, actor: Actor, item_id: str) -> dict[str, Any]:
        raw, path, acl = self._open_item(actor, item_id)
        return self._record(raw, path, acl)

    def _held_item(self, actor: Actor, item_id: Any) -> tuple[Mapping[str, Any], str, dict]:
        """The item ``list_folder`` is handing to its ``each`` (its metadata, canonical
        path and ACL read moments ago in that listing), audience- and ACL-checked for
        ``actor`` again without I/O; any other item is opened (:meth:`_open_item`)."""
        held = self._in_hand
        if held is None or held[0].get("id") != item_id:
            return self._open_item(actor, item_id)
        self._gate(actor)
        raw, path, acl = held
        keys = self.resources(path, self._is_folder(raw))
        if not (self._audience.allows_all(actor, keys)
                and self._audience.visible_all(actor, keys, acl)):
            raise NotVisible()
        return raw, path, acl

    def get_content(self, actor: Actor, item_id: str, *, max_bytes: int = 20_000_000,
                    content_route: bool = False,
                    may_retry: Callable[[], bool] | None = None,
                    from_listing: bool = False) -> dict[str, Any]:
        """Original bytes of one visible file (bounded), with a content-hash citation.

        The download URL is requested only after the visibility check, for the
        same eTag and canonical path (``content_route``: through
        ``graph.drive_item_content`` and its 302, ``SourceApi.get_content``); bytes
        come credential-free from the tenant's download hosts; the eTag and path
        are re-read afterwards so a moving item never yields a mixed citation.
        ``may_retry`` (a caller's budget) is asked before the one download retry
        (:meth:`_download_same_version`). ``from_listing``: called from ``list_folder``'s
        ``each`` for the child it hands over, the item is not opened again (its metadata
        and permissions GETs): the listing's own reads of them, moments old, stand, and
        the download's metadata read still requires the listed eTag and path. Any other
        item, or outside that call, is opened as without it.
        """
        raw, path, acl = (self._held_item(actor, item_id) if from_listing
                          else self._open_item(actor, item_id))
        size = raw.get("size")
        if not isinstance(raw.get("file"), Mapping):
            raise SourceUnavailable("drive item is not a file")
        if type(size) is not int or size < 0:
            raise SourceUnavailable("drive item size invalid")
        if size > max_bytes:
            return {"record": self._record(raw, path, acl), "content": None,
                    "content_status": "exceeds_cap", "max_bytes": max_bytes}
        if content_route:
            content = self._api.get_content(
                grammar.url("graph.drive_item_content", drive=self.drive_id, item=raw["id"]),
                max_bytes=max(1, max_bytes))
        else:
            content = self._download_same_version(raw, path, max_bytes, may_retry)
        if len(content) != size:
            raise SourceUnavailable("downloaded bytes differ from item size")
        after = self._metadata(raw["id"])
        if after.get("eTag") != raw.get("eTag") or self._source_path(after) != path:
            raise SourceUnavailable("drive item changed during content read")
        digest = sha256(content).hexdigest()
        hashes = (raw.get("file") or {}).get("hashes") or {}
        listed = hashes.get("sha256Hash") if isinstance(hashes, Mapping) else None
        if isinstance(listed, str) and listed.lower() != digest:
            raise SourceUnavailable("downloaded bytes differ from the source sha256Hash")
        listed = hashes.get("quickXorHash") if isinstance(hashes, Mapping) else None
        if isinstance(listed, str) and listed != quick_xor_hash(content):
            raise SourceUnavailable("downloaded bytes differ from the source quickXorHash")
        return {"record": self._record(after, path, acl, digest), "content": content,
                "content_status": "read", "content_sha256": digest}

    def get_version_content(self, actor: Actor, item_id: str, version_id: str, *,
                            max_bytes: int = 20_000_000) -> dict[str, Any]:
        """Bytes of one named historical version of a visible file (bounded).

        The item's own visibility and ACL check comes first (:meth:`_open_item`); the
        bytes come through ``graph.drive_item_version_content`` and its 302
        (``SourceApi.get_content``, credential-free on the tenant's download hosts).
        Graph lists no hash for a past version, so the caller compares the bytes with
        what its pin recorded; ``record`` describes the item as it is now.
        """
        raw, path, acl = self._open_item(actor, item_id)
        if not isinstance(raw.get("file"), Mapping):
            raise SourceUnavailable("drive item is not a file")
        content = self._api.get_content(
            grammar.url("graph.drive_item_version_content", drive=self.drive_id,
                        item=raw["id"], version=version_id), max_bytes=max(1, max_bytes))
        digest = sha256(content).hexdigest()
        return {"record": self._record(raw, path, acl, digest), "content": content,
                "content_status": "read", "content_sha256": digest,
                "version_id": version_id}

    DOWNLOAD_ATTEMPTS = 2

    def _download_same_version(self, raw: Mapping[str, Any], path: str, max_bytes: int,
                               may_retry: Callable[[], bool] | None = None) -> bytes:
        """Bytes of the exact version ``raw`` names, with one bounded retry.

        Only a transient transport failure is retried (``is_transport_failure``: a
        body cut short in transit, ``DownloadIncomplete``, or no status at all:
        ``URLError``, timeout, reset, TLS), once, and only while ``may_retry()``
        allows it; an HTTP status is never retried. Each attempt first re-reads
        the item for a fresh short-lived download URL and requires the same eTag
        and canonical path, so a retry never mixes versions: an item that changed
        fails closed. The second failure is raised as it came.
        """
        for attempt in range(self.DOWNLOAD_ATTEMPTS):
            with_url = self._metadata(raw["id"], download_url=True)
            if with_url.get("eTag") != raw.get("eTag") or self._source_path(with_url) != path:
                raise SourceUnavailable("drive item changed during content read")
            download = with_url.get(grammar.DRIVE_DOWNLOAD_URL_KEY)
            if not isinstance(download, str) or not download:
                raise SourceUnavailable("drive item lacks a download URL")
            secret_url = self._transport.register_secret(Secret(download))
            try:
                return self._transport.download_preauthenticated(
                    secret_url.reveal(), max_bytes=max(1, max_bytes))
            except SourceUnavailable as exc:
                if (not is_transport_failure(exc) or attempt + 1 >= self.DOWNLOAD_ATTEMPTS
                        or (may_retry is not None and not may_retry())):
                    raise
            finally:
                self._transport.forget_secret(secret_url)
                del download, with_url
        raise AssertionError("unreachable")  # pragma: no cover

    def resolve_citation(self, handle: str, actor: Actor, *,
                         verify_content: bool = False) -> dict[str, Any]:
        (drive_id, item_id), etag, extra = parse_handle(handle, DRIVE_SCHEME, 2)
        if drive_id != self.drive_id:
            raise NotVisible()
        cited_sha = extra.get("sha256")
        if verify_content and cited_sha:
            read = self.get_content(actor, item_id)
            current, current_sha = read["record"], read.get("content_sha256")
        else:
            current, current_sha = self.get_item(actor, item_id), None
        same = current["etag"] == etag and (current_sha is None or current_sha == cited_sha)
        return {"citation": handle, "status": "current" if same else "changed_since",
                "cited_version": etag, "current_version": current["etag"],
                "current_citation": drive_citation(self.drive_id, item_id, current["etag"],
                                                   current_sha),
                "content_verified": bool(current_sha), "record": current}

    @staticmethod
    def _list_identity(raw: Mapping[str, Any]) -> tuple[str, str, str]:
        """``sharepointIds`` (siteId, listId, listItemId) of a drive item."""
        ids = raw.get("sharepointIds")
        if not isinstance(ids, Mapping):
            raise SourceUnavailable("drive item lacks SharePoint list identity")
        found = tuple(ids.get(key) for key in ("siteId", "listId", "listItemId"))
        if any(not isinstance(value, str) or not value or len(value) > 512
               or any(ord(c) < 33 for c in value) for value in found):
            raise SourceUnavailable("drive item SharePoint list identity invalid")
        return found  # type: ignore[return-value]

    def list_item_fields(self, actor: Actor, item_id: str, *,
                         field_names: Sequence[str]) -> dict[str, Any]:
        """Current SharePoint column values of one visible file, for the named columns only.

        Same guarded entry as every item read: drive gate (no I/O without a
        grant), a metadata-only GET, audience and source ACL on the
        server-derived path. Only then is the file's own listItem read
        (``grammar.drive_list_item_fields_url``: GET
        ``/drives/{drive}/items/{item}/listItem?expand=fields``). The drive
        eTag (it covers metadata as well as content) and the list identity
        are read before and after, so a moving item never yields a mixed
        snapshot (``SourceUnavailable``).

        ``field_names`` is the caller's column allowlist (the Portal taxonomy
        for doc-metadata); every other field is dropped and only counted.
        Returns ``snapshot`` in exactly the stored ``sharepoint-listitem-fields/v1``
        shape (``source_api`` = the request URL = the document's listItem URL
        rule; ``drive_etag`` = the file's eTag), plus the citation, the content
        tag ``ctag`` and file ``source_hashes`` (so a caller can bind the columns
        to bytes read earlier: a column edit moves the eTag, not the content) and
        counts.
        Values are returned to the caller only; nothing here prints or logs them.
        """
        if isinstance(field_names, (str, bytes)):
            raise ValueError("field_names must be a list of column names")
        names = frozenset(field_names)
        if not names or len(names) > 100 or not all(grammar.column_name_ok(n) for n in names):
            raise ValueError("field_names must be 1..100 plain column names")
        raw, path, acl = self._open_item(actor, item_id, sharepoint_ids=True)
        if not isinstance(raw.get("file"), Mapping):
            raise SourceUnavailable("drive item is not a file")
        etag = raw.get("eTag")
        if not isinstance(etag, str) or not etag:
            raise SourceUnavailable("drive item lacks eTag")
        identity = self._list_identity(raw)
        url = grammar.drive_list_item_fields_url(self.drive_id, item_id)
        try:
            listed = self._api.get_json(url)
        except SourceNotFound:
            raise NotVisible() from None
        if not isinstance(listed, Mapping) or listed.get("id") != identity[2]:
            raise SourceUnavailable("SharePoint listItem identity mismatch")
        listed_ids = listed.get("sharepointIds")
        if isinstance(listed_ids, Mapping) and any(
                listed_ids.get(key) != value for key, value in
                zip(("siteId", "listId", "listItemId"), identity)):
            raise SourceUnavailable("SharePoint listItem scope mismatch")
        fields = listed.get("fields")
        if not isinstance(fields, Mapping):
            raise SourceUnavailable("SharePoint listItem fields unavailable")
        after = self._metadata(item_id, sharepoint_ids=True)
        if (after.get("eTag") != etag or self._list_identity(after) != identity
                or self._source_path(after) != path):
            raise SourceUnavailable("drive item changed during listItem read")
        kept = jsonable({name: fields[name] for name in sorted(names) if name in fields})
        try:
            encoded = json.dumps(kept, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise SourceUnavailable("SharePoint listItem fields are not finite JSON") from None
        if len(encoded.encode("utf-8")) > 64_000:
            raise SourceUnavailable("SharePoint listItem fields exceed bound")
        withheld = sum(1 for key in fields if isinstance(key, str) and key not in names
                       and not key.startswith("@odata."))
        list_item_etag = listed.get("eTag")
        return {
            "snapshot": {"source": "sharepoint-listitem-fields/v1", "site_id": identity[0],
                         "list_id": identity[1], "list_item_id": identity[2],
                         "drive_etag": etag, "source_api": url, "fields": kept},
            "drive_id": self.drive_id, "item_id": item_id, "path": path,
            "citation": drive_citation(self.drive_id, item_id, etag),
            # content identity of the same metadata read: a column edit changes the eTag,
            # never the cTag or the file hashes (a reader binds columns to cited bytes)
            "ctag": raw.get("cTag") if isinstance(raw.get("cTag"), str) else None,
            "source_hashes": jsonable(dict(raw["file"].get("hashes") or {}))
            if isinstance(raw["file"].get("hashes"), Mapping) else {},
            "list_item_etag": list_item_etag if isinstance(list_item_etag, str) else None,
            "fields_requested": sorted(names), "fields_present": sorted(kept),
            "fields_withheld_count": withheld, "source_acl": dict(acl)}

    def library_columns(self, actor: Actor, columns: Sequence[Mapping[str, Any]], *,
                        max_items: int = 200) -> dict[str, Any]:
        """Which of ``columns`` this library defines and how often its newest documents
        fill them.

        ``columns`` are column specs ``{name, display_name}`` (the Portal taxonomy,
        ``operating.doc_metadata_taxonomy.COLUMNS``). GET only: the drive's backing
        list (``graph.drive_list``), its column definitions (``graph.list_columns``)
        and its list items (``graph.drive_list_items``, the drive's own list only,
        ``$expand=fields($select=<the defined columns' internal names>)``).

        A spec is defined when its name or display name equals any definition's name
        or display name, the Portal's provisioning rule (plx-customer-portal
        scripts/document-metadata/provision-sharepoint-columns.mjs:141-149 @bc6e8de);
        the item read selects the internal name of the matching definition.

        The items are walked across pages (up to ``LIBRARY_WALK_ITEMS``) and the
        sample is the newest ``max_items`` documents (content type ``0x0101...``) by
        ``lastModifiedDateTime``, then list item id; folders and other items are
        counted apart. Per spec: ``missing`` (no definition, listing complete),
        ``unknown`` (no definition seen while the listing was cut, or none of the
        sample fills it while the walk was cut before the list's end, so newer
        documents were not read), ``empty`` (defined, none of the newest documents
        fills it), ``defined_unreadable`` (matched definition without a plain
        internal name) or ``filled``, with ``filled`` = sampled documents whose value
        carries something (``FILL_RULE``: a boolean ``false`` counts as filled).
        Counts and states only, plus ``site_name`` (the library site's URL name,
        for the caller to match against names it already holds); no value, internal
        name, id or URL is returned.

        The list read is library-wide, so per-item audience and ACL cannot be
        decided one by one: the actor must be covered for the whole drive
        (``LiveSourceAudience.covers_whole``), else ``NotVisible`` before any I/O.
        """
        specs = [(c.get("name"), c.get("display_name")) for c in columns
                 if isinstance(c, Mapping)]
        if (not specs or len(specs) != len(columns) or len(specs) > 100
                or not all(grammar.column_name_ok(n) and isinstance(d, str) for n, d in specs)):
            raise ValueError("columns must be 1..100 specs with plain names")
        if not isinstance(max_items, int) or not 1 <= max_items <= 1000:
            raise ValueError("max_items must be 1..1000")
        listed, site_id, list_id = self._whole_library(actor)
        defined, listing = self._api.get_pages(
            grammar.url("graph.list_columns", {"$select": "name,displayName"},
                        site=site_id, list=list_id), max_pages=10, max_items=2000)
        found: dict[str, tuple[str, str | None]] = {}  # spec -> (matched_by, internal name)
        for name, display in specs:
            hit = _column_definition(defined, name, display)
            if hit is not None:
                internal = hit[1].get("name")
                found[name] = (hit[0], internal if grammar.column_name_ok(internal) else None)
        selected = sorted({internal for _how, internal in found.values() if internal})
        sample = {"items": 0, "documents_read": 0, "documents": 0, "folders": 0, "other": 0,
                  "documents_undated": 0, "documents_without_fields": 0, "complete": True,
                  "order": "lastModifiedDateTime desc"}
        filled = dict.fromkeys(selected, 0)
        if selected:
            items, walk = self._api.get_pages(self._list_items_url(selected),
                                              max_pages=50, max_items=LIBRARY_WALK_ITEMS)
            documents = []
            for item in items:
                kind = item.get("contentType")
                kind = kind.get("id") if isinstance(kind, Mapping) else None
                kind = kind.upper() if isinstance(kind, str) else ""
                if kind.startswith("0X0120"):
                    sample["folders"] += 1
                elif kind.startswith("0X0101"):
                    documents.append(item)
                else:
                    sample["other"] += 1
            newest = sorted(documents, key=_newest_first)[:max_items]
            sample.update(items=len(items), documents_read=len(documents),
                          documents=len(newest), complete=walk["complete"],
                          documents_undated=sum(_modified(d) is None for d in documents))
            for item in newest:
                fields = item.get("fields")
                if not isinstance(fields, Mapping):
                    sample["documents_without_fields"] += 1
                    continue
                for internal in selected:
                    filled[internal] += int(_column_filled(fields.get(internal)))
        report: dict[str, Any] = {}
        for name, _display in specs:
            how, internal = found.get(name, (None, None))
            if how is None:
                state, count = ("missing" if listing["complete"] else "unknown"), None
            elif internal is None:
                state, count = "defined_unreadable", None
            else:
                count = filled[internal]
                state = "filled" if count else "empty" if sample["complete"] else "unknown"
            report[name] = {"state": state, "matched_by": how, "filled": count}
        return {"columns_defined": len(defined), "columns_listing_complete": listing["complete"],
                "sample": sample, "columns": report,
                "site_name": _site_url_name(listed.get("webUrl"), site_id)}

    def library_item_fields(self, actor: Actor, files: Mapping[str, str], *,
                            field_names: Sequence[str], max_pages: int,
                            late: Callable[[], bool] | None = None) -> dict[str, Any]:
        """The named columns of the files ``files`` (drive item id -> the eTag the caller
        holds) of this library, from its list items read GET-only in pages: the
        library-wide counterpart of ``list_item_fields`` for a caller with many files.

        Same entry as ``library_columns`` (drive gate, the actor covered for the whole
        drive, else ``NotVisible`` before any I/O; the drive's own list, never a site or
        list id). The list items are read in pages of 200 with ``$expand=fields($select=
        <field_names>),driveItem($select=id,eTag)`` until every file is bound, the list
        ends, ``max_pages`` pages were read or ``late()`` answers true before a page. A
        row binds only when its drive item has a requested id and exactly that eTag and
        its fields are an object; an id seen twice binds neither. Every other row is
        dropped unread, and every other column of a bound row; a file whose id or eTag is
        not text is never bound.

        Returns ``snapshots`` (item id -> the ``sharepoint-listitem-fields/v1`` shape of
        ``list_item_fields``: site and list id from the drive's list, ``source_api`` =
        the first page request, ``drive_etag`` = the bound eTag), ``pages_read`` and
        ``stopped``: None, ``page_cap`` or ``budget_cut``. A failure after the list read
        raises with ``snapshots`` and ``pages_read`` of the pages read before it.
        Values are returned to the caller only; nothing here prints or logs them."""
        if isinstance(field_names, (str, bytes)):
            raise ValueError("field_names must be a list of column names")
        names = sorted(frozenset(field_names))
        if not names or len(names) > 100 or not all(grammar.column_name_ok(n) for n in names):
            raise ValueError("field_names must be 1..100 plain column names")
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
            raise ValueError("max_pages must be 1..1000")
        files = {i: e for i, e in files.items() if isinstance(i, str) and isinstance(e, str)}
        _listed, site_id, list_id = self._whole_library(actor)
        found: dict[str, dict] = {}
        twice: set[str] = set()
        read, stopped = 0, None
        url: str | None = self._list_items_url(names, drive_item=True)
        source_api = url
        try:
            while url is not None and len(found) + len(twice) < len(files):
                if late is not None and late():
                    stopped = "budget_cut"
                    break
                if read >= max_pages:
                    stopped = "page_cap"
                    break
                rows, url = self._api.get_page(url)
                read += 1
                for row in rows:
                    item = row.get("driveItem")
                    iid = item.get("id") if isinstance(item, Mapping) else None
                    if not isinstance(iid, str) or iid not in files or iid in twice:
                        continue
                    if iid in found:
                        del found[iid]
                        twice.add(iid)
                        continue
                    fields = row.get("fields")
                    if (item.get("eTag") != files[iid] or not isinstance(fields, Mapping)
                            or not isinstance(row.get("id"), str) or not row["id"]):
                        continue
                    found[iid] = {"source": "sharepoint-listitem-fields/v1",
                                  "site_id": site_id, "list_id": list_id,
                                  "list_item_id": row["id"], "drive_etag": files[iid],
                                  "source_api": source_api,
                                  "fields": jsonable({n: fields[n] for n in names
                                                      if n in fields})}
        except (NotVisible, SourceUnavailable, ValueError) as exc:
            exc.snapshots, exc.pages_read = found, read
            raise
        return {"snapshots": found, "pages_read": read, "stopped": stopped}

    def _whole_library(self, actor: Actor) -> tuple[Mapping[str, Any], str, str]:
        """The drive's own backing list for a library-wide read, (the list, site id, list
        id): drive gate, then the actor covered for the whole drive
        (``LiveSourceAudience.covers_whole``, per-item audience and ACL cannot be decided
        on a list read), else ``NotVisible`` before any I/O; one GET of
        ``graph.drive_list``."""
        self._gate(actor)
        if not self._audience.covers_whole(actor, self.resource("/")):
            raise NotVisible()
        try:
            listed = self._api.get_json(grammar.url("graph.drive_list",
                                                    {"$select": "id,parentReference,webUrl"},
                                                    drive=self.drive_id))
        except SourceNotFound:
            raise NotVisible() from None
        parent = listed.get("parentReference") if isinstance(listed, Mapping) else None
        list_id = listed.get("id") if isinstance(listed, Mapping) else None
        site_id = parent.get("siteId") if isinstance(parent, Mapping) else None
        if not grammar.TYPES["guid"].valid(list_id) or not grammar.TYPES["site_id"].valid(site_id):
            raise SourceUnavailable("drive list identity invalid")
        return listed, site_id, list_id

    def _list_items_url(self, columns: Sequence[str], *, drive_item: bool = False) -> str:
        """The first page of the drive's list items (``graph.drive_list_items``, 200 a
        page) with only ``columns`` of each row's fields, and its drive item's id and
        eTag when ``drive_item``."""
        expand = "fields($select=" + ",".join(columns) + ")"
        if drive_item:
            expand += ",driveItem($select=id,eTag)"
        return grammar.url("graph.drive_list_items", {"$expand": expand, "$top": "200"},
                           drive=self.drive_id)


# The list items a library column read walks (``SourceApi.get_pages``' item bound, 25
# pages of 200) before it takes its newest-first sample.
LIBRARY_WALK_ITEMS = 5000
# How a sampled document's column value counts (``_column_filled``), stated in the output.
FILL_RULE = {"null": "empty", "blank_text": "empty", "empty_list": "empty",
             "false": "filled", "zero": "filled", "other": "filled"}


def _column_definition(defined: Sequence[Mapping[str, Any]], name: str, display: str
                       ) -> tuple[str, Mapping[str, Any]] | None:
    """(the spec field that matched, the definition) of the first definition whose name
    or display name equals the spec's name, then its display name; an internal-name
    equality wins over a display-name one."""
    for how, wanted in (("name", name), ("display_name", display)):
        for key in ("name", "displayName"):
            for definition in defined:
                if definition.get(key) == wanted:
                    return how, definition
    return None


# A Graph UTC timestamp: up to 7 fraction digits (.NET ticks), which
# ``datetime.fromisoformat`` refuses before Python 3.11; the 7th digit is dropped.
_GRAPH_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,7}))?Z")


def _modified(item: Mapping[str, Any]) -> datetime | None:
    stamp = item.get("lastModifiedDateTime")
    match = _GRAPH_STAMP.fullmatch(stamp) if isinstance(stamp, str) else None
    if match is None:
        return None
    *parts, fraction = match.groups()
    try:
        return datetime(*map(int, parts), int((fraction or "0")[:6].ljust(6, "0")),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def _newest_first(item: Mapping[str, Any]) -> tuple:
    """Sort key: dated documents newest first, then by list item id (newest first);
    an undated document sorts after every dated one."""
    when = _modified(item)
    item_id = item.get("id")
    number = int(item_id) if isinstance(item_id, str) and item_id.isdigit() else -1
    return (when is None, -(when.timestamp() if when else 0), -number)


def _site_url_name(web_url: Any, site_id: str) -> str | None:
    """The ``/sites/<name>`` or ``/teams/<name>`` URL name of a list's web URL on the
    site id's own host; None for a root site or any other shape."""
    if not isinstance(web_url, str) or "," not in site_id:
        return None
    parts = urlsplit(web_url)
    found = re.match(r"^/(?:sites|teams)/([^/]+)(?:/|$)", unquote(parts.path))
    if (parts.scheme != "https" or (parts.hostname or "").lower()
            != site_id.split(",", 1)[0].lower() or not found
            or not grammar.TYPES["site_name"].valid(found[1])):
        return None
    return found[1]


def _column_filled(value: Any) -> bool:
    """A list item field value that carries something (``False`` and ``0`` do: ``FILL_RULE``)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, Mapping)):
        return bool(value)
    return True


# ------------------------------------------------------------------ site resolution

def resolve_site_drive(api: SourceApi, host: str, site_name: str) -> str:
    """The default document library (drive id) of the site ``/sites/<site_name>``
    on ``host``, resolved at run time by two metadata GETs of the closed grammar
    (``graph.site_by_path``, ``graph.site_drive``); nothing is hardcoded.

    ``host`` must be the configured site's host (``sharepoint_host_from_env``).
    A returned site on another host, or an id that is not a well-formed
    site/drive id, is refused (``SourceUnavailable``). A missing site raises
    ``SourceNotFound`` (HTTP 404). No audience decision is made here: the
    returned drive is read only through a ``SharePointReader`` (audience and
    item permissions per item)."""
    if not grammar.TYPES["sp_host"].valid(host) or not grammar.TYPES["site_name"].valid(
            site_name):
        raise ValueError("site host or name invalid")
    site = api.get_json(grammar.url("graph.site_by_path", {"$select": "id"}, host=host,
                                    name=site_name))
    site_id = site.get("id") if isinstance(site, Mapping) else None
    if (not grammar.TYPES["site_id"].valid(site_id) or "," not in site_id
            or site_id.split(",", 1)[0].lower() != host.lower()):
        raise SourceUnavailable("graph site resolution returned an invalid site id")
    drive = api.get_json(grammar.url("graph.site_drive", {"$select": "id"}, site=site_id))
    drive_id = drive.get("id") if isinstance(drive, Mapping) else None
    if not grammar.TYPES["drive_id"].valid(drive_id):
        raise SourceUnavailable("graph site resolution returned an invalid drive id")
    return drive_id


SITE_SELECT = "id,name,webUrl"
LIBRARY_SELECT = "id,name,driveType"
_SITE_PATH = re.compile(r"^/(?:sites|teams)/([^/]+)/?$")
# A OneDrive's path on the tenant's OneDrive host: ``/personal/<segment>``, the segment
# derived from its owner's UPN (``people_exclusion.onedrive_path``).
_PERSONAL_PATH = re.compile(r"^/personal/([^/]+)/?$")


class SharePointSites:
    """GET-only SharePoint estate enumeration on the configured host ``host``.

    ``sites`` lists every site the application may read (``graph.sites_all``,
    ``/sites/getAllSites``, paged); ``libraries`` lists one site's drives
    (``graph.site_drives``, paged); ``drive`` resolves a site's default library by
    name (:func:`resolve_site_drive`). Every returned id is checked against the
    grammar; a malformed one is dropped and counted (``provenance_dropped``),
    never passed on. No audience decision is made here: a library is read only
    through a ``SharePointReader`` (audience and item permissions per item)."""

    def __init__(self, api: SourceApi, host: str) -> None:
        if not grammar.TYPES["sp_host"].valid(host):
            raise ValueError("site host invalid")
        self._api, self.host = api, host.lower()
        # The people-exclusion guard (``build_graph`` attaches it to the api): an
        # excluded person's OneDrive is refused before its libraries are listed.
        self.people = people_exclusion.from_api(api)

    def __repr__(self) -> str:
        return "SharePointSites()"

    def prepare_people(self) -> None:
        """Make the people guard ready (its ID list, then one directory listing that
        names nobody); ``ExclusionUnavailable`` when it cannot be."""
        self.people.require()
        self.people.bind(self._api)

    def drive(self, site_name: str) -> str:
        return resolve_site_drive(self._api, self.host, site_name)

    def sites(self, *, max_items: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """(sites as ``{id, host, name, personal}``, completeness); ``name`` is the site
        URL name of ``/sites/<name>`` or ``/teams/<name>`` (None for a root site),
        ``personal`` the segment of a ``/personal/<segment>`` OneDrive path (else None;
        held only to refuse an excluded owner's OneDrive, never reported)."""
        raws, completeness = self._api.get_pages(
            grammar.url("graph.sites_all", {"$select": SITE_SELECT}), max_pages=50,
            max_items=max_items)
        sites, dropped = [], 0
        for raw in raws:
            site_id = raw.get("id")
            if not grammar.TYPES["site_id"].valid(site_id) or "," not in site_id:
                dropped += 1
                continue
            url = raw.get("webUrl") if isinstance(raw.get("webUrl"), str) else ""
            found = _SITE_PATH.match(unquote(urlsplit(url).path))
            name = found[1] if found and grammar.TYPES["site_name"].valid(found[1]) else None
            owned = _PERSONAL_PATH.match(unquote(urlsplit(url).path))
            sites.append({"id": site_id, "host": site_id.split(",", 1)[0].lower(),
                          "name": name, "personal": owned[1].lower() if owned else None})
        return sites, {**completeness, "provenance_dropped": dropped}

    def libraries(self, site_id: str, *, max_items: int
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """(the site's drives as ``{id, name, drive_type}``, completeness)."""
        raws, completeness = self._api.get_pages(
            grammar.url("graph.site_drives", {"$select": LIBRARY_SELECT}, site=site_id),
            max_pages=10, max_items=max_items)
        libraries, dropped = [], 0
        for raw in raws:
            if not grammar.TYPES["drive_id"].valid(raw.get("id")):
                dropped += 1
                continue
            name, kind = raw.get("name"), raw.get("driveType")
            libraries.append({"id": raw["id"], "name": name if isinstance(name, str) else None,
                              "drive_type": kind if isinstance(kind, str) else None})
        return libraries, {**completeness, "provenance_dropped": dropped}


# ------------------------------------------------------------------ factory + probe

def build_graph(environ: Mapping[str, str] | None = None, *, sender=None,
                clock=None, retry: BoundedBackoff | None = None
                ) -> tuple[SourceApi, GuardedTransport]:
    """The Graph ``SourceApi`` over one ``GuardedTransport``. ``retry`` opts
    into the bounded GET retry (429/502/503/504 and transport failures,
    ``Retry-After`` honoured up to its cap); the mail capture uses it."""
    env = os.environ if environ is None else environ
    # The download host is pinned to the configured site's host (the first part
    # of a composite SHAREPOINT_SITE_ID); without it content reads are refused.
    kwargs = {"environ": env, "sharepoint_host": sharepoint_host_from_env(env)}
    transport = GuardedTransport(sender, **kwargs) if sender else GuardedTransport(**kwargs)
    credentials = ClientCredentials.from_env("MICROSOFT_GRAPH", env)
    tokens = TokenProvider(credentials, GRAPH_SCOPE, transport,
                           **({"clock": clock} if clock else {}))
    api = SourceApi("graph", tokens, transport, retry=retry)
    # The people-exclusion guard every Graph capture shares (mail, chats, OneDrive):
    # from PLX_EXCLUDED_PEOPLE_OIDS / PLX_EXCLUDED_PEOPLE_FILE; absent, it fails closed.
    api.people = people_exclusion.PeopleExclusion.from_env(env)
    return api, transport


# Graph's own codes (``live_source_guard.upstream_error_code`` tokens) of a 403 that
# refuses an application permission: the directory's Authorization_RequestDenied (live
# 2026-10-19 on ``/users``) and Exchange's ErrorAccessDenied (Mail.Read missing or an
# application access policy). Narrower than the captures' rule (``graph_block`` blocks
# every 401/403): a probe check is blocked only by a refused grant; a 401, a 403 with
# any other code or none, and every other failure stay failures.
PERMISSION_REFUSALS = frozenset({"authorization_request_denied", "error_access_denied"})
# The application permission each refused probe read needs, named in its part.
PROBE_PERMISSIONS = {"graph.users": "User.Read.All", "graph.user": "User.Read.All",
                     "graph.mail_folder": "Mail.Read"}


def probe_block(part: str, op: str, exc: BaseException) -> dict[str, Any] | None:
    """The ``blocked_upstream`` part (``capture_mail.graph_block``'s shape: error_code
    ``missing_permission``, cause ``host_misconfiguration``, the permission ``op`` needs)
    of a probe read that failed with ``exc``, or None unless Graph answered it 403 with
    a ``PERMISSION_REFUSALS`` code."""
    from .capture_mail import graph_block

    status = getattr(exc, "http_status", None)
    if not isinstance(exc, SourceUnavailable) or status != 403 \
            or getattr(exc, "error_code", None) not in PERMISSION_REFUSALS:
        return None
    return graph_block(part, op, PROBE_PERMISSIONS, status)


def exclusion_later_ask(exc: BaseException) -> bool:
    """Whether ``exc`` is the people guard refusing for its own configuration (the ID
    list absent, invalid or unreadable: later_ask ``excluded-people-oids``), the one
    refusal a person's answer clears. An unresolved guard (the directory listing that
    binds addresses failed or was truncated) waits on Graph, not on that ask."""
    return (isinstance(exc, people_exclusion.ExclusionUnavailable) and exc.__cause__ is None
            and exc.code != "excluded_people_unresolved")


def mail_block(exc: BaseException, transport: GuardedTransport) -> dict[str, Any] | None:
    """``probe_block`` of a mail listing (``GraphMailReader.list_messages``): its mailbox
    lookup (``mail.mailbox_lookup``) until ``transport`` read ``graph.user``, then its
    folder and message reads (``mail.folder_read``), the parts ``capture_mail`` names.
    The people guard refusing the mailbox before any request for it, for want of its
    ID list (``exclusion_later_ask``), is its typed block
    (``mail.people_exclusion``, later_ask ``excluded-people-oids``; codes only); one whose
    directory listing Graph refused is that refusal (``mail.user_listing``), and any other
    unresolved guard is no block."""
    from .capture_mail import REFUSED_PARTS

    if isinstance(exc, people_exclusion.ExclusionUnavailable):
        cause = exc.__cause__
        if isinstance(cause, SourceUnavailable):
            return probe_block("mail.user_listing", "graph.users", cause)
        if not exclusion_later_ask(exc):
            return None
        return people_exclusion.PeopleExclusion(unavailable=exc.code).block_part(
            "mail.people_exclusion")
    part, op = REFUSED_PARTS["folder" if "graph.user" in transport.operations_read
                             else "mailbox"]
    return probe_block(part, op, exc)


def _shape(items: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted({key for item in items for key in item})


def probe(environ: Mapping[str, str] | None = None, *, sender=None, mailbox: str | None = None,
          content: bool = False, max_content_bytes: int = 1_000_000) -> dict[str, Any]:
    """Counts and shapes only; never prints subjects, bodies, names or ids. A users or
    mail read refused for a missing grant (``probe_block``) is a ``blocked_upstream``
    part and ``ok`` false; the SharePoint parts are read as without it."""
    env = os.environ if environ is None else environ
    api, transport = build_graph(env, sender=sender)
    report: dict[str, Any] = {"source": "microsoft-graph", "evidence_label": "live-read",
                              "read_only_guard": "GET-only; token POST only"}
    blocked: list[dict[str, Any]] = []
    try:
        users = api.get_json(grammar.url("graph.users", {"$top": "1", "$select": "id"}))
    except SourceUnavailable as exc:
        # Graph's own refusal code: the token was granted and the GET answered.
        found = probe_block("mail.user_listing", "graph.users", exc)
        if found is None:
            raise
        blocked.append(found)
        users = None
    report["token"] = "ok"
    user_values = users.get("value", []) if isinstance(users, Mapping) else []
    if users is not None:
        report["users_page_count"] = len(user_values)
    target = mailbox or (user_values[0].get("id") if user_values else None)
    if target:
        probe_audience = LiveSourceAudience({"mail:*": ["probe:operator"]}, tenant="probe")
        actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
        reader = GraphMailReader(api, probe_audience)
        try:
            listing = reader.list_messages(actor, target, page_size=1, max_pages=1,
                                           max_items=1)
        except (SourceUnavailable, people_exclusion.ExclusionUnavailable) as exc:
            found = mail_block(exc, transport)
            if found is None:
                raise
            blocked.append(found)
            target = None
    if target:
        items = listing["items"]
        mail = {"mailbox_source": "argument" if mailbox else "first-user",
                "messages_returned": len(items), "completeness": listing["completeness"],
                "record_keys": _shape(items),
                "has_change_key": all(bool(i.get("change_key")) for i in items) if items else None}
        if items:
            resolved = reader.resolve_citation(items[0]["citation"], actor)
            mail["citation_roundtrip"] = resolved["status"]
        report["mail"] = mail
    site_id, drive_id = env.get("SHAREPOINT_SITE_ID"), env.get("SHAREPOINT_DRIVE_ID")
    if site_id:
        site = api.get_json(grammar.url("graph.site", {"$select": "id"}, site=site_id))
        drives = api.get_json(grammar.url("graph.site_drives", {"$select": "id"}, site=site_id))
        report["site"] = {"site_ok": isinstance(site, Mapping) and bool(site.get("id")),
                          "drives_count": len(drives.get("value", []))
                          if isinstance(drives, Mapping) else None}
    if drive_id:
        audience = LiveSourceAudience({f"sharepoint:{drive_id}:/*": ["probe:operator"]},
                                      tenant="probe")
        actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
        reader = SharePointReader(api, audience, drive_id, transport=transport,
                                  read_permissions=True)
        listing = reader.list_folder(actor, "/", max_pages=2, max_items=200)
        items = listing["items"]
        files = [i for i in items if i["kind"] == "file"]
        drive = {"root_children": len(items),
                 "folders": sum(1 for i in items if i["kind"] == "folder"),
                 "files": len(files), "completeness": listing["completeness"],
                 "record_keys": _shape(items),
                 "permissions_status": sorted({i["source_acl"]["status"] for i in items})}
        if items:
            drive["citation_roundtrip"] = reader.resolve_citation(items[0]["citation"],
                                                                  actor)["status"]
        if content and files:
            small = [f for f in files if isinstance(f.get("size"), int)
                     and f["size"] <= max_content_bytes]
            if small:
                read = reader.get_content(actor, small[0]["id"], max_bytes=max_content_bytes)
                drive["content_probe"] = {"status": read["content_status"],
                                          "bytes": len(read["content"] or b""),
                                          "sha256_cited": "#sha256=" in read["record"]["citation"]}
            else:
                drive["content_probe"] = {"status": "no_root_file_under_cap"}
        report["sharepoint"] = drive
    report["download_host_pinned"] = transport.sharepoint_host is not None
    report["requests_sent"] = transport.requests_sent
    report["operations_read"] = sorted(transport.operations_read)
    if blocked:
        report.update(ok=False, blocked_upstream=blocked)
    return report


def listitem_probe(environ: Mapping[str, str] | None = None, *, sender=None, drive: str,
                   folder: str, max_items: int = 20) -> dict[str, Any]:
    """SharePoint column read of every file in one folder: names and counts only.

    A probe audience grants one probe principal the folder's top-level tree.
    For each listed file (up to ``max_items``) the listItem fields are read
    through ``SharePointReader.list_item_fields`` with the Portal-taxonomy
    column allowlist (``operating.doc_metadata.CURRENT_COLUMN_FIELDS``). The
    report carries counts, the allowlisted column names seen and booleans
    (eTag captured, cTag equal to the listing's, ``source_api`` equal to the
    listItem URL rule) and, per document-type column, how its value reads for
    a playbook kind (``portal_kind_columns.column_reading``: establishes,
    consistent, another_document, ...); never a field value, file name, id or
    URL. ``taxonomy_columns_by_library_class``: under the library's security
    class (``doc_metadata_taxonomy.known_library_class`` of its display name,
    one GET; STANDARD when the name names none; UNRESOLVED when it cannot be
    read), the files and listItems read and, per Portal-taxonomy column, how
    many of them carry it (0: empty or not provisioned).
    """
    from operating import doc_metadata_taxonomy as taxonomy  # data only; no I/O
    from operating.doc_metadata import CURRENT_COLUMN_FIELDS  # data only; no I/O

    if not isinstance(max_items, int) or not 1 <= max_items <= 50:
        raise ValueError("max_items must be 1..50")
    if not grammar.TYPES["drive_id"].valid(drive) or not grammar.TYPES["drive_path"].valid(folder):
        raise ValueError("drive or folder invalid")
    env = os.environ if environ is None else environ
    api, transport = build_graph(env, sender=sender)
    top = "/" + folder.strip("/").split("/", 1)[0]
    audience = LiveSourceAudience({f"sharepoint:{drive}:{top}/*": ["probe:operator"]},
                                  tenant="probe")
    actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
    reader = SharePointReader(api, audience, drive, transport=transport)
    listing = reader.list_folder(actor, folder, page_size=200, max_pages=2, max_items=400)
    files = [item for item in listing["items"] if item["kind"] == "file"][:max_items]
    try:
        library_class = taxonomy.known_library_class(reader.library(actor)["name"]) or "STANDARD"
    except (SourceUnavailable, NotVisible):
        library_class = taxonomy.UNRESOLVED_CLASS
    report: dict[str, Any] = {
        "check": "graph.sharepoint_listitem", "source": "sharepoint-listitem",
        "evidence_label": "injected-transport" if sender else "live-read",
        "read_only_guard": "GET-only; token POST only",
        "fields_requested": sorted(CURRENT_COLUMN_FIELDS), "files_listed": len(files),
        "listing_completeness": listing["completeness"]}
    from operating.portal_kind_columns import KIND_COLUMNS, column_reading  # data only

    read, etag_ok, api_ok, ctag_ok, failed = 0, 0, 0, 0, []
    present: dict[str, int] = {}
    readings: dict[str, int] = {}
    withheld = 0
    for item in files:
        try:
            result = reader.list_item_fields(actor, item["id"],
                                             field_names=CURRENT_COLUMN_FIELDS)
        except (SourceUnavailable, NotVisible) as exc:
            failed.append(type(exc).__name__)
            continue
        read += 1
        snapshot = result["snapshot"]
        etag_ok += int(snapshot["drive_etag"] == item["etag"])
        api_ok += int(grammar.drive_list_item_fields_url_ok(snapshot["source_api"], drive,
                                                            item["id"]))
        # the content tag the case binds a document-type column to (playbook_originals)
        ctag_ok += int(isinstance(result["ctag"], str) and result["ctag"] == item.get("ctag"))
        withheld += result["fields_withheld_count"]
        for name in result["fields_present"]:
            present[name] = present.get(name, 0) + 1
        for column in KIND_COLUMNS:  # how each recorded type value reads; never the value
            if column in snapshot["fields"]:
                how = column_reading(column, snapshot["fields"][column])["reads_as"]
                readings[f"{column}:{how}"] = readings.get(f"{column}:{how}", 0) + 1
    report.update(listitems_read=read, drive_etag_matches_listing=etag_ok,
                  ctag_matches_listing=ctag_ok, document_kind_readings=readings,
                  source_api_rule_ok=api_ok, read_failures=sorted(failed),
                  field_names_present=sorted(present), field_presence_counts=present,
                  fields_withheld_total=withheld, library_class=library_class,
                  taxonomy_columns_by_library_class={library_class: {
                      "files": len(files), "listitems_read": read,
                      "columns": {c["name"]: present.get(c["name"], 0)
                                  for c in taxonomy.COLUMNS}}})
    report["ok"] = bool(files) and read == len(files) and etag_ok == read and api_ok == read
    report["requests_sent"] = transport.requests_sent
    return report


def library_columns_probe(environ: Mapping[str, str] | None = None, *, sender=None,
                          max_items: int = 200, registry: str | None = None) -> dict[str, Any]:
    """The Portal-taxonomy columns of the Portal's own document library: the drive the
    environment names (``SHAREPOINT_DRIVE_ID``, never an argument), read through
    ``SharePointReader.library_columns`` for a probe actor covering the whole drive.

    The report gives the library's class (``doc_metadata_taxonomy.known_library_class``
    of its display name), the column definitions counted, the walk and its newest-first
    sample by kind and, per Portal-taxonomy column, ``missing`` / ``unknown`` /
    ``empty`` / ``filled`` / ``defined_unreadable`` (``columns_by_state``) with the
    count of sampled documents filling it (``fill_counts``, by ``fill_rule``: a boolean
    false counts as filled; only an ``empty`` or ``filled`` column has one, so a count
    of 0 there always means empty). With ``registry`` (a ``plx-source-registry/v1`` file),
    ``registry_sites.read`` names the library's site when one of the registry's
    ``sharepoint`` ``site`` entries names it (names already committed there), else
    ``registry_sites.unlisted`` counts it. A read failure is reported by class, never
    text; ``ok`` needs the read and a complete column listing.
    """
    from operating import doc_metadata_taxonomy as taxonomy  # data only; no I/O

    if not isinstance(max_items, int) or not 1 <= max_items <= 1000:
        raise ValueError("max_items must be 1..1000")
    names = None
    if registry is not None:
        from .capture_sharepoint import CaptureError, registry_site_names
        try:
            names = registry_site_names(registry)
        except (CaptureError, OSError, ValueError):
            raise ValueError("registry unreadable") from None
    env = os.environ if environ is None else environ
    drive = env.get("SHAREPOINT_DRIVE_ID")
    report: dict[str, Any] = {
        "check": "graph.sharepoint_library_columns", "source": "sharepoint-library-columns",
        "evidence_label": "injected-transport" if sender else "live-read",
        "read_only_guard": "GET-only; token POST only", "fill_rule": dict(FILL_RULE),
        "ok": False}
    if not grammar.TYPES["drive_id"].valid(drive):
        return {**report, "status": "not_configured"}
    api, transport = build_graph(env, sender=sender)
    audience = LiveSourceAudience({f"sharepoint:{drive}:/*": ["probe:operator"]},
                                  tenant="probe")
    actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
    reader = SharePointReader(api, audience, drive, transport=transport)
    try:
        library_class = taxonomy.known_library_class(reader.library(actor)["name"]) or "STANDARD"
    except (SourceUnavailable, NotVisible):
        library_class = taxonomy.UNRESOLVED_CLASS
    report["library_class"] = library_class
    try:
        found = reader.library_columns(actor, taxonomy.COLUMNS, max_items=max_items)
    except (SourceUnavailable, NotVisible) as exc:
        return {**report, "status": type(exc).__name__, "requests_sent": transport.requests_sent}
    site = found.pop("site_name")
    states = {state: sorted(n for n, c in found["columns"].items() if c["state"] == state)
              for state in ("missing", "unknown", "empty", "filled", "defined_unreadable")}
    report.update(found, status="read", ok=found["columns_listing_complete"],
                  columns_by_state={k: v for k, v in states.items() if v},
                  fill_counts={n: c["filled"] for n, c in found["columns"].items()
                               if c["state"] in ("empty", "filled")})
    if names is not None:
        listed = [n for n in names if site is not None and n.casefold() == site.casefold()]
        report["registry_sites"] = {"read": listed[:1], "unlisted": 0 if listed else 1}
    report["requests_sent"] = transport.requests_sent
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.graph_read",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("probe", help="read-only counts/shapes (no content printed)")
    run.add_argument("--mailbox", help="UPN or object id; default: first user")
    run.add_argument("--content", action="store_true",
                     help="also read one root file <= 1 MB and report its byte count only")
    columns = commands.add_parser("listitem-probe",
                                  help="read the Portal-taxonomy SharePoint columns of the files "
                                       "in one folder; names and counts only (no values)")
    columns.add_argument("--drive", required=True, help="drive id")
    columns.add_argument("--folder", required=True, help="drive-relative folder path")
    columns.add_argument("--max-items", type=int, default=20)
    library = commands.add_parser("library-columns-probe",
                                  help="the Portal-taxonomy column definitions and a newest-first "
                                       "list-item sample of the SHAREPOINT_DRIVE_ID library; "
                                       "states and counts only (no values)")
    library.add_argument("--max-items", type=int, default=200,
                         help="newest documents sampled (1..1000)")
    library.add_argument("--registry", default=None,
                         help="plx-source-registry/v1 file: name the library's site when a "
                              "sharepoint site entry names it (registry_sites)")
    capture = commands.add_parser("mail-capture",
                                  help="versioned mail capture (delta, bodies, attachments) "
                                       "into Workbench state; counts and completeness only")
    from .capture_mail import add_cli_arguments
    add_cli_arguments(capture)
    drives = commands.add_parser("sharepoint-capture",
                                 help="versioned SharePoint capture (drive items, permissions, "
                                      "bytes) into Workbench state; counts and completeness only")
    from .capture_sharepoint import add_cli_arguments as add_sharepoint_arguments
    add_sharepoint_arguments(drives)
    teams = commands.add_parser("teams-capture",
                                help="versioned Teams capture (channel and chat messages, "
                                     "membership as audience) into Workbench state; counts, "
                                     "completeness and blocked parts only")
    from .capture_teams import add_cli_arguments as add_teams_arguments
    add_teams_arguments(teams)
    lists = commands.add_parser("sharepoint-list-capture",
                                help="versioned SharePoint list-item capture (the lists the "
                                     "scope names, item permissions as audience) into "
                                     "Workbench state; counts, completeness and blocked parts "
                                     "only")
    from .capture_sharepoint_lists import add_cli_arguments as add_list_arguments
    add_list_arguments(lists)
    breadth = commands.add_parser("capture-breadth",
                                  help="one capture cycle over every allowlisted mailbox "
                                       "(inbox and sent items) and the SharePoint scope, run "
                                       "twice; per-folder completeness and counts only")
    from .capture_breadth import add_cli_arguments as add_breadth_arguments
    add_breadth_arguments(breadth)
    every = commands.add_parser("mail-all-mailboxes",
                                help="mail capture over every directory mailbox but the "
                                     "excluded people's and the HR / payroll deny list; "
                                     "counts, completeness and the request log's "
                                     "excluded_requested only")
    from .mail_directory_scope import add_cli_arguments as add_every_arguments
    add_every_arguments(every)
    args = parser.parse_args(argv)
    try:
        if args.command == "mail-capture":
            from .capture_mail import run_cli
            result = run_cli(args, environ=environ, sender=sender)
        elif args.command == "sharepoint-capture":
            from .capture_sharepoint import run_cli as run_sharepoint_cli
            result = run_sharepoint_cli(args, environ=environ, sender=sender)
        elif args.command == "teams-capture":
            from .capture_teams import run_cli as run_teams_cli
            result = run_teams_cli(args, environ=environ, sender=sender)
        elif args.command == "sharepoint-list-capture":
            from .capture_sharepoint_lists import run_cli as run_list_cli
            result = run_list_cli(args, environ=environ, sender=sender)
        elif args.command == "mail-all-mailboxes":
            from .mail_directory_scope import run_cli as run_every_cli
            result = run_every_cli(args, environ=environ, sender=sender)
        elif args.command == "capture-breadth":
            from .capture_breadth import run_cli as run_breadth_cli
            result = run_breadth_cli(args, environ=environ, sender=sender)
        elif args.command == "listitem-probe":
            result = listitem_probe(environ, sender=sender, drive=args.drive,
                                    folder=args.folder, max_items=args.max_items)
        elif args.command == "library-columns-probe":
            result = library_columns_probe(environ, sender=sender, max_items=args.max_items,
                                           registry=args.registry)
        else:
            result = probe(environ, sender=sender, mailbox=args.mailbox, content=args.content)
    except ReadOnlyGuardError as exc:
        print(json.dumps({"error": "read_only_guard", "message": str(exc)}), file=sys.stderr)
        return 2
    except SourceUnavailable as exc:
        # Fixed-vocabulary diagnostics (HTTP status, upstream error code, detail),
        # never the message: a 403's class must reach the live record.
        print(json.dumps({"error": type(exc).__name__, **failure_diagnostics(exc)}),
              file=sys.stderr)
        return 1
    except (NotVisible, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    # A capture whose only failure is typed incomplete scope (blocked_upstream, cause
    # later_ask) exits 0: the verifier records it blocked, not passed.
    blocked = args.command == "sharepoint-capture" and bool(result.get("blocked_upstream"))
    return 0 if result.get("ok", True) or blocked else 1


__all__ = ["GraphMailReader", "SharePointReader", "SharePointSites", "build_graph",
           "drive_citation", "exclusion_later_ask", "library_columns_probe", "listitem_probe", "mail_block",
           "mail_citation", "main", "permission_acl", "probe", "probe_block",
           "resolve_site_drive"]


if __name__ == "__main__":
    raise SystemExit(main())
