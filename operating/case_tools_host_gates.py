"""Read gates of ``CaptureCaseHostTools``: captured-mail and SharePoint search gates,
refusals, projected-name checks and live re-reads.

Split from ``operating/case_tools_host.py``; moved unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence
from operating.capture_age import capture_decides
from operating.ontology_tools_evidence import RuntimeCitations
from .case_tools_host_rules import (
    LIVE_READ_GETS, REREAD_GETS, _READ_OK, _RECORDED_ACL, _UNREAD, _current_version,
    _mail_store,
)
from .case_tools_host_citations import _ACTIVE_CALL, _PAGE_BUILD, _carried_withheld, _member_container


def _set_key(fast: Any) -> tuple:
    """What a call keeps of the people exclusion's set (``_set_ahead``): the set's own identity
    (its list, its version, owner view or not), never the object, which is built anew each time
    for the owner (an ``id`` never matched: every handle was looked up alone)."""
    return ("people-excluded-versions", type(fast).__name__, fast.exclusion, fast.covered_ver)


class _GatesMixin:
    """Methods of ``CaptureCaseHostTools`` (``case_tools_host_gates``); mixed in there, never used alone."""

    def _item_gate(self, runtime_gate: Callable[[Any, str], bool] | None
                   ) -> Callable[[Any, str], bool]:
        citations = RuntimeCitations(self.runtime, mail_capture_dir=self.mail_capture_dir)

        def fallback(actor: Any, handle: str) -> bool:
            return runtime_gate is not None and runtime_gate(actor, handle) is True

        def gate(actor: Any, handle: str) -> bool:
            handle = str(handle)
            fast = self._full_reader_set(actor, handle)
            if fast is not None:
                # A full reader (business rule 2026-11-17): of mail, Teams and meetings only
                # the people exclusion's precomputed set withholds (no capture read), for
                # a handle the set covers (served at or below its version number, nothing
                # newer); one recorded since, or not served, goes through the gate below.
                container = _member_container(handle)
                versions, held = self._set_kept(fast, handle, container)
                if fast.covered(handle, container, versions):
                    withheld = fast.withholds(handle, container, held)
                    if withheld is not None:   # None: unreadable, the gate decides
                        return not withheld
            if handle.startswith("sharepoint://") and self.sharepoint_capture_dir is not None:
                return self.captured(actor, handle) is not None
            if not handle.startswith("graph-mail://") or self.mail_capture_dir is None:
                return fallback(actor, handle)
            # A member of a captured zip/.msg/.eml attachment is visible iff its
            # attachment is: every check below decides on the attachment, then the mail
            # the citation carries (DOC-008) on the citation itself.
            cited, handle = handle, _member_container(handle) or handle
            from ontology.people_directory import parse_person_handle
            if parse_person_handle(handle) is not None:
                # A person (``people_directory``): seen through any mailbox it corresponds
                # in that this reader is served, decided by the one full check.
                return citations.read(handle, actor, content=False).get("status") in _READ_OK
            bound = self._call_bound
            if bound.search or _PAGE_BUILD.get():
                # This host's decisions in this call (the bound is shared by every gate
                # built inside the call, of any host). A page build (``page_build``)
                # decides its mail citations the same way: the same decision as the
                # full check below, made once per call on the capture's rows.
                memo = bound.memo.setdefault(("host", id(self)), {"host": self})
                if "opened" not in memo:   # one store check per search call
                    opened = self._mail()
                    memo["opened"] = None if opened is None else opened[1]
                audience = memo["opened"]
                if audience is None:
                    return False
                stores = lambda: _mail_store(self.runtime,  # noqa: E731
                                             Path(self.mail_capture_dir))
                return self._mail_search_gate(citations, stores, audience, actor, handle,
                                              memo) and not _carried_withheld(
                    citations, stores, cited, memo)
            opened = self._mail()
            if opened is None:
                return False
            stores, audience = opened
            try:
                with stores as store:
                    gated = self.runtime.employee_gate(actor)
                    # the citation itself: a member through its container's gate
                    if not store.item_gate(audience, citations._people())(gated, cited):
                        return False
            except Exception:  # noqa: BLE001 - a refused gate reads nothing
                return False
            # the status alone decides: a whole attachment's text is never decoded here;
            # the citation itself is read (a member: its container's gate, then the mail
            # it is or sits under, DOC-008)
            return citations.read(cited, actor, content=False).get("status") in _READ_OK

        def prefetch(actor: Any, handles: Iterable[str]) -> None:
            """Read ahead, in batches, what a search's mail gate reads of each mail
            citation of ``handles`` (``_mail_facts``: the rows one gate call reads alone,
            kept in this call's memo), so the gate's per-candidate calls find them read.
            Decides nothing and records nothing: every candidate is still gated one by
            one, exactly as before. Outside a search call, or without a mail capture,
            nothing is read."""
            bound = self._call_bound
            if not (bound.search or _PAGE_BUILD.get()):
                return
            handles = [str(h) for h in handles]
            if self._set_ahead(actor, handles):
                return   # the set decides them all: no capture read ahead
            handles = [_member_container(h) or h for h in handles]
            files = [h for h in handles if h.startswith("sharepoint://")]
            if files and self.sharepoint_capture_dir is not None and bound.search:
                self._sharepoint_ahead(files)   # a page build gates its files as before
            mail = [h for h in handles if h.startswith("graph-mail://")]
            if not mail or self.mail_capture_dir is None:
                return
            memo = bound.memo.setdefault(("host", id(self)), {"host": self})
            with _mail_store(self.runtime, Path(self.mail_capture_dir)) as store:
                self._mail_facts(store, mail, memo)

        gate.prefetch = prefetch   # type: ignore[attr-defined]
        # What decides this gate for a reader beyond the host's stores (``item_gate_scope``):
        # the runtime gate's own material; none when it states none (no audience key).
        inner = getattr(runtime_gate, "key_material", None)

        def key_material(actor: Any) -> Any:
            # A full reader with a usable set: ``("people-excluded", tag)``, the key the
            # session's ``ontology.sq_item_gate`` decides mail, Teams and meetings by
            # (``people_excluded.reader_key``); the owner ``("people-excluded-owner",
            # tag)`` (the set withholds nothing); anyone else: the runtime gate's own.
            from operating.pg.people_excluded import OwnerView
            fast = self._full_reader_set(actor, "graph-mail://")
            if fast is not None:
                return ("people-excluded-owner" if isinstance(fast, OwnerView)
                        else "people-excluded", fast.exclusion)
            return None if inner is None else inner(actor)

        gate.key_material = key_material  # type: ignore[attr-defined]

        def full_reader_tag(actor: Any) -> str:
            # ``people_excluded.full_reader``'s answer alone: the exclusion tag of a full
            # reader with a usable set, else "" (never the runtime gate's own material,
            # which reads the staff rule's SharePoint baselines: ~1 s on a session's
            # first call, 2026-11-18, only to be told the reader is no full reader)
            fast = self._full_reader_set(actor, "graph-mail://")
            return fast.exclusion if fast is not None and isinstance(
                fast.exclusion, str) else ""

        gate.full_reader_tag = full_reader_tag  # type: ignore[attr-defined]

        def owner_reader(actor: Any) -> bool:
            # THE owner (``EmployeeEntitlement.owner``): the set withholds nothing from it
            return self._owner(self.runtime.employee_gate(actor))

        gate.owner_reader = owner_reader  # type: ignore[attr-defined]
        # The case runtime the inner gate decides with (``item_gate_from_case_runtime``):
        # the bulk readers compute each reader's parts with its functions (plx.sql's Teams
        # chat windows and transcript rule, plx.table's mail and Teams rules). Without it
        # every one of them failed closed on a served host: plx.sql showed every reader,
        # the owner too, 0 Teams messages (2026-11-18).
        gate.runtime = getattr(runtime_gate, "runtime", None)  # type: ignore[attr-defined]
        return gate

    def _set_kept(self, fast: Any, handle: str, container: str | None) -> tuple[Any, Any]:
        """(the served version numbers ``fast.covered`` reads, the set's handles
        ``fast.withholds`` reads) for ``handle``: from this call's memo when a read ahead
        (``_set_ahead``) kept them all, else (None, None) (they read for themselves)."""
        bound = self._call_bound
        if not (bound.search or _PAGE_BUILD.get()):
            return None, None
        from operating.pg.people_excluded import related, withhold_names
        kept = bound.memo.get(_set_key(fast))
        if kept is None:
            return None, None
        names, versions, held = kept
        wanted = set(related(handle, container)) | set(withhold_names(handle, container))
        if not wanted <= names:
            return None, None
        return versions, held

    def _set_ahead(self, actor: Any, handles: list[str]) -> bool:
        """For a full reader with a usable set, read the served version numbers and the
        set's own rows of every handle the set decides among ``handles`` (two index
        statements, never the whole set), kept in this call's memo (``_set_kept``). True
        when the set decides and covers every handle (nothing else to read ahead)."""
        from operating.pg import people_excluded
        mine = [h for h in handles if people_excluded.decides(h)]
        fast = self._full_reader_set(actor, mine[0]) if mine else None
        if fast is None:
            return False
        names: set[str] = set()
        for h in mine:
            container = _member_container(h)
            names.update(people_excluded.related(h, container))
            names.update(people_excluded.withhold_names(h, container))
        key = _set_key(fast)
        memo = self._call_bound.memo
        kept = memo.get(key)
        if kept is not None:
            names |= kept[0]
        versions = people_excluded.latest_versions(names)
        held = None if versions is None else fast.withheld(names)
        if versions is None or held is None:
            memo.pop(key, None)
            return False
        memo[key] = (frozenset(names), versions, held)
        return len(mine) == len(handles) and all(
            fast.covered(h, _member_container(h), versions) for h in mine)

    def _full_reader_set(self, actor: Any, handle: str) -> Any:
        """The people exclusion's precomputed set (``operating.pg.people_excluded``) when
        it decides ``handle`` for ``actor`` alone: the reader is a full reader
        (``_unrestricted``), the handle's scheme is one the set covers and the set is
        usable now (Postgres serves, built for this very exclusion list). Else None: the
        gate decides as before (fail closed)."""
        from operating.pg import people_excluded
        if not people_excluded.decides(handle):
            return None
        bound = self._call_bound
        if (bound.search or _PAGE_BUILD.get()) and (self._in_call or _ACTIVE_CALL.get() is not None):
            # one decision per reader in a call: the set, the reader's gate and its level
            # are read once, not once per handle (a list of 1,100 matters decided in 0.8 s of
            # them); a call is seconds long and ``_set_ahead`` keeps its versions the same way
            key = ("full-reader-set", actor)
            if key not in bound.memo:
                bound.memo[key] = self._full_reader_set_now(actor)
            return bound.memo[key]
        return self._full_reader_set_now(actor)

    def _full_reader_set_now(self, actor: Any) -> Any:
        from operating.pg import people_excluded
        try:
            got = people_excluded.excluded()
            gated = self.runtime.employee_gate(actor)
            if got is None or not self._unrestricted(gated):
                return None
            if self._owner(gated):
                # the owner: the same coverage, nothing withheld
                return people_excluded.OwnerView(got.exclusion, got.covered_ver)
        except Exception:  # noqa: BLE001 - undecidable: the gate decides
            return None
        return got

    @staticmethod
    def _mail_facts(store: Any, handles: Iterable[str], memo: dict) -> None:
        """For each mail citation of ``handles`` (a message, or an attachment of one, not a
        page), what ``_mail_search_gate`` reads of its message, read in batches and kept
        in ``memo`` under ``("mail_facts", store path, handle)``: the message handle and
        its latest captured row's mailbox, sender fields (``from``, ``sender``) and
        recipient fields (``to``, ``cc``, ``bcc``: the participant rule decides them all), or
        None for no such message (an attachment no row names, or a message with no row).
        The same rows the per-handle reads take (``store._latest``: the highest version
        id of the handle; the attachment's message handle), only fewer statements; an
        attachment whose rows name two messages is left to its own read."""
        from integrations.graph_evidence import is_mail_attachment_citation
        from ontology.mentions import cited_page
        path = str(store.path)
        todo = [h for h in dict.fromkeys(handles) if ("mail_facts", path, h) not in memo
                and cited_page(h)[1] is None]
        attachments = [h for h in todo if "#" in h and is_mail_attachment_citation(h)]
        messages: dict[str, str | None] = {h: h for h in todo if h not in set(attachments)}
        for at in range(0, len(attachments), 900):
            chunk = attachments[at:at + 900]
            found: dict[str, set[str]] = {}
            for row in store.db.execute(
                    "SELECT l1_handle, message_handle FROM attachments WHERE l1_handle IN "
                    "(SELECT value FROM json_each(?))", (json.dumps(chunk),)):
                found.setdefault(row[0], set()).add(row[1])
            for handle in chunk:
                named = found.get(handle)
                if named is None:
                    messages[handle] = None
                elif len(named) == 1:
                    messages[handle] = next(iter(named))
        wanted = sorted({m for m in messages.values() if m is not None})
        rows: dict[str, tuple[Any, ...]] = {}
        # a message naming an undecided address has no row here: no reader's (facts None)
        from integrations.capture_mail import undecided_free_sql
        free = undecided_free_sql(store.db)
        for at in range(0, len(wanted), 900):
            for row in store.db.execute(
                    "SELECT l1_handle, mailbox_id, json_extract(record, '$.from', '$.sender', "
                    "'$.to', '$.cc', '$.bcc') "
                    "FROM messages WHERE version_id IN (SELECT MAX(version_id) FROM messages "
                    "WHERE l1_handle IN (SELECT value FROM json_each(?)) GROUP BY l1_handle) "
                    f"AND {free}",
                    (json.dumps(wanted[at:at + 900]),)):
                named = json.loads(row[2]) if row[2] is not None else [None] * 5
                rows[row[0]] = (row[1], *named)
        for handle, message in messages.items():
            facts = None if message is None or message not in rows else (message,
                                                                         *rows[message])
            memo[("mail_facts", path, handle)] = facts

    def _mail_search_gate(self, citations: Any, stores: Callable[[], Any], audience: Any, actor: Any,
                          handle: str, memo: dict) -> bool:
        """The mail item gate for a search candidate (``_CallBound.search``): the same
        decision as the capture's item gate followed by ``RuntimeCitations.mail_capture``
        (both read the one mail capture store with the live audience: the mailbox
        audience and owner ACL recorded at capture, the case mail scope, the employee's
        item ACL, the people exclusion over the mailbox and every participant), made on the
        capture's rows alone: never read live, no message body or attachment text
        decoded, and the reader's gate, its visible mailboxes and each mailbox's decision
        made once per call (``memo``). A page citation is decided by the full capture
        resolve (only its text tells whether the page exists)."""
        from integrations.graph_evidence import is_mail_attachment_citation
        from ontology.mentions import cited_page
        try:
            key = ("gated", actor)
            if key not in memo:
                memo[key] = self.runtime.employee_gate(actor)
            gated = memo[key]
            if citations._exclusion_refusal() or not audience.may_read_any(gated, "mail:"):
                return False
            if cited_page(handle)[1] is not None:
                with stores() as store:
                    if not store.item_gate(audience, citations._people())(gated, handle):
                        return False
                # the page must be there: its reading's kept page count says so, the
                # text never decoded (as the full gate's read answers it)
                return citations.mail_capture(handle, gated, audience, text=True,
                                              page_exists=True) is not None
            with stores() as store:
                # What the gate reads of the handle's message (``_mail_facts``: its mailbox
                # and senders, not the whole record), once per call.
                if ("mail_facts", str(store.path), handle) not in memo:
                    self._mail_facts(store, [handle], memo)
                ahead = memo.get(("mail_facts", str(store.path), handle), _UNREAD)
                if ahead is None:
                    return False   # no message: what the reads below find too
                message = handle if ahead is _UNREAD else ahead[0]
                if ahead is _UNREAD and "#" in handle and is_mail_attachment_citation(handle):
                    found = store.db.execute("SELECT message_handle FROM attachments "
                                             "WHERE l1_handle=?", (handle,)).fetchone()
                    if found is None:
                        return False
                    message = found[0]
                # a message and each of its attachments are one decision (several of
                # its attachments, or it and its attachments, are candidates of one search)
                kept = ("message", str(store.path), message, gated, id(audience))
                if kept in memo:
                    return memo[kept]
                seen = ("visible", str(store.path), gated, id(audience))
                if seen not in memo:
                    memo[seen] = (audience, frozenset(store._visible(gated, audience)))
                if ahead is _UNREAD:
                    row = store._latest(message, readable=True)
                    if row is None:
                        memo[kept] = False
                        return False
                    record = json.loads(row["record"])
                    mailbox, sent = row["mailbox_id"], [record.get("from"),
                                                        record.get("sender")]
                    to = [record.get(k) for k in ("to", "cc", "bcc")]
                else:
                    mailbox, sent, to = ahead[1], [ahead[2], ahead[3]], list(ahead[4:7])
                if mailbox not in memo[seen][1]:
                    memo[kept] = False
                    return False
                decided = ("mailbox", str(store.path), mailbox, gated)
                if decided not in memo:
                    memo[decided] = citations._visible_mailbox(
                        store, mailbox, gated, audience) is not None
                if not memo[decided]:
                    memo[kept] = False
                    return False
                memo[kept] = not citations.participants_withheld(store, sent, to, memo,
                                                                 mailbox=mailbox, gated=gated)
                return memo[kept]
        except Exception:  # noqa: BLE001 - a refused gate or unreadable capture admits nothing
            return False

    # ---- SharePoint: the per-item gate ----------------------------------------

    def captured(self, actor: Any, handle: str, *, text: bool = False,
                 _whole: bool = True) -> dict[str, Any] | None:
        """The captured version ``handle`` names (``SharePointCaptureStore.resolve``
        shape) when this reader may be served it now, else None (module docstring).
        A refusal of a version the capture holds (or of the reader) is recorded for this
        call in ``_refused`` as (status, reason, detail); a version the capture does not
        hold for this reader is not."""
        from integrations.capture_sharepoint import CaptureError
        store, audience = self._sharepoint(), self._audience()
        if store is None or audience is None:
            return None
        handle = str(handle)
        self._refused.pop(handle, None)
        self._decided.discard(handle)
        container = _member_container(handle)
        if container is not None:
            return self._captured_member(actor, handle, container, text)
        item_refused = ("withheld", "sharepoint_item_refused",
                        "the Workbench's SharePoint capture holds no version of this file "
                        "this reader may be served now (not visible, deleted, moved or gone "
                        "from the capture's scope); it is not read live in its place")
        try:
            gated = self.runtime.employee_gate(actor)
        except Exception:  # noqa: BLE001 - a refused employee gate reads nothing
            self._refused[handle] = ("withheld", "employee_gate",
                                     "the employee gate refused this reader")
            return None
        try:
            # the runtime's people guard decides the mail a file carries (DOC-008);
            # ``_whole=False``: a member's container (the member decides its own)
            view = store.resolve(handle, gated, audience, text=text,
                                 ahead=self._read_ahead(store),
                                 people=getattr(self.runtime, "people_exclusion", None),
                                 _whole=_whole)
            if view is None:
                # Not captured, or not visible to this reader at capture: decided only
                # when the capture holds this exact version as the item's current one.
                if _current_version(store, handle):
                    self._decided.add(handle)
                return None
            # The employee's item ACL decides on this version and on the item's latest
            # captured one: a revocation captured since hides the older versions too,
            # also where no live re-read can confirm it (no reader, budget, failure).
            removed = view["removed_reason"] is not None
            denied = not removed and any(
                self.runtime._employee_acl_denial(gated, "sharepoint",
                                                  {**view["row"], "source_acl": acl})
                for acl in (view["source_acl"], view["item_source_acl"]))
            if removed or denied:
                self._refused[handle] = item_refused
                if denied:
                    self._decided.add(handle)
                return None
            if self._projected_stale(view):
                # Checked after the reader's permissions: a reader who may not see the
                # file learns nothing from this reason.
                self._refused[handle] = (
                    "unresolvable", "capture_stale",
                    "the Workbench's index shows this file under a name or path the "
                    "capture no longer holds (moved since the index was built); it is "
                    "served again once the index is rebuilt, and it is not read live in "
                    "its place")
                return None
            decided = self._live_check(gated, view)
        except (CaptureError, sqlite3.Error, OSError, ValueError, LookupError):
            self._refused[handle] = ("unresolvable", "capture_unreadable",
                                     "the Workbench's SharePoint capture could not be read "
                                     "for this file now; it is not read live in its place")
            return None
        if decided is None:
            self._refused[handle] = item_refused
            if (frozenset(gated.principals), view["l1_handle"]) \
                    in self._call_bound.live_decided:
                self._decided.add(handle)
            return None
        basis, changed, failure = decided
        if failure == "search":
            # A search serves the stored row at any capture age, decided on the
            # permissions the capture recorded (as the projection decides every other
            # answer), labelled with its capture time. A row whose capture holds no read,
            # non-empty permission list (this version's or the item's latest) is
            # withheld at any age: an empty recorded list decides nothing.
            if not self._unrestricted(gated) and not all(
                    isinstance(acl, Mapping) and acl.get("status") in _RECORDED_ACL
                    and isinstance(acl.get("identities"), list) and acl["identities"]
                    for acl in (view["source_acl"], view["item_source_acl"])):
                self._refused[handle] = (
                    "withheld", "capture_permissions_unknown",
                    "the Workbench's capture holds no permission list for this file, so "
                    "a search cannot decide who may see it; it is not listed")
                return None
        elif basis != "live":
            if not capture_decides(view["last_seen_at"], self.clock(),
                                   self.max_capture_age) \
                    and not self._full_read_stale(gated, view):
                # Capture-time permissions too old (or undated, or dated after now) to
                # decide alone.
                why = ("this call's live re-read bound is reached" if failure == "budget"
                       else f"the live re-read failed now ({failure.partition(':')[2][:80]})"
                       if failure else "this host has no live reader for the drive")
                if failure == "budget":
                    self._over_budget.add(handle)
                self._refused[handle] = (
                    "unresolvable", "capture_too_old",
                    f"the captured file is outside the host's capture age bound and "
                    f"{why}, so its recorded permissions cannot decide; it is not read "
                    f"live in its place")
                return None
        row = view["row"]
        self._served[view["l1_handle"]] = {
            "captured_at": view["last_seen_at"], "basis": basis, "changed": changed,
            "failure": failure, "text_status": row.get("text_status"),
            "content_status": row.get("content_status"), "size": row.get("size")}
        return view

    def _unrestricted(self, gated: Any) -> bool:
        """The reader's owner-open flag (``employee_permissions.reader_unrestricted``),
        decided once per tool call and reader (``_CallBound.memo``)."""
        from .employee_permissions import reader_unrestricted
        memo = self._call_bound.memo
        key = ("unrestricted", frozenset(getattr(gated, "principals", ())))
        if key not in memo:
            memo[key] = reader_unrestricted(getattr(self.runtime, "employee_gate", None),
                                            gated)
        return memo[key]

    def _owner(self, gated: Any) -> bool:
        """Whether the reader is THE owner (``employee_permissions.reader_owner``), decided
        once per tool call and reader (``_CallBound.memo``)."""
        from .employee_permissions import reader_owner
        memo = self._call_bound.memo
        key = ("owner", frozenset(getattr(gated, "principals", ())))
        if key not in memo:
            memo[key] = reader_owner(getattr(self.runtime, "employee_gate", None), gated)
        return memo[key]

    def _full_read_stale(self, gated: Any, view: Mapping[str, Any]) -> bool:
        """A file past the capture-age bound served on its recorded permissions only under
        the reader's declared full-read grant (``CaseRuntime.full_read_stale_allows``):
        both recorded permission lists (this version's and the item's latest) name only
        decidable people none of whom is excluded, a recorded last modifier and a OneDrive
        owner are decidable and cleared, and the exclusion is loaded. Anyone else: False
        (unchanged)."""
        allows = getattr(self.runtime, "full_read_stale_allows", None)
        acls = (view.get("source_acl"), view.get("item_source_acl"))
        if not callable(allows) or not all(isinstance(a, Mapping) and isinstance(
                a.get("identities"), list) for a in acls):
            return False
        row = view.get("row") if isinstance(view.get("row"), Mapping) else {}
        listed = {"identities": [*acls[0]["identities"], *acls[1]["identities"]]}
        # A file in a personal OneDrive: its site path segment names the owner.
        url = row.get("original_url")
        owner = re.search(r"/personal/([^/?#]+)", url) if isinstance(url, str) else None
        return allows(gated, "sharepoint", listed, view.get("last_seen_at"),
                      modifier=row.get("last_modified_by"),
                      onedrive_owner=owner.group(1) if owner else None)

    def _captured_member(self, actor: Any, handle: str, container: str, text: bool
                         ) -> dict[str, Any] | None:
        """A member of a captured zip/.msg/.eml file (``integrations.captured_text``):
        served iff its container is served to this reader now (:meth:`captured` on the
        container: item ACL and file class, staleness, the live re-read and the capture
        age bound all decide on the container's view), then read from the container's
        pinned bytes. A refusal of the container is the member's."""
        from integrations.capture_sharepoint import CaptureError
        held = self.captured(actor, container, _whole=False)
        if held is None:
            if container in self._refused:
                self._refused[handle] = self._refused[container]
            if container in self._decided:
                self._decided.add(handle)
            return None
        try:
            view = self._sharepoint().resolve(
                handle, self.runtime.employee_gate(actor), self._audience(), text=text,
                people=getattr(self.runtime, "people_exclusion", None))
        except (CaptureError, sqlite3.Error, OSError, ValueError, LookupError):
            self._refused[handle] = ("unresolvable", "capture_unreadable",
                                     "the Workbench's SharePoint capture could not be read "
                                     "for this file now; it is not read live in its place")
            return None
        if view is not None:
            self._served[view["l1_handle"]] = self._served[held["l1_handle"]]
        return view

    def refusals(self) -> dict[str, str]:
        """The last tool call's refusals of captured versions the capture holds: version
        handle -> reason (``sharepoint_item_refused``, ``capture_too_old``, ...). For an
        in-process diagnosis of a reader's own answers (``operating.sharepoint_host_check``)
        and for ``CaseCaptures`` (a refused version is unresolvable, one the capture does
        not hold is read live); never part of a tool result."""
        return {handle: reason for handle, (_status, reason, _detail) in self._refused.items()}

    def refusal_kind(self, handle: str) -> str:
        """Of the last :meth:`captured` of ``handle`` that served nothing: ``REFUSED``
        when this reader's permissions decided it (the item ACL or the capture's
        audience refused a version the capture holds as current, or a live re-read said
        not visible or denied), else ``FAILED`` (an employee gate that refused, a store
        that could not be read, a stale index, outside a search a capture too old to
        decide (a search decides on the capture at any age), in a search a stored row
        with no recorded permission list (``capture_permissions_unknown``), a removed
        item, no capture, a live 404 or 410, a live permission list not read in full
        such as on a 429, 5xx or timeout: ``graph_read.undecided_not_visible``). For
        ``CaseCaptures.gate_decision`` only; never in a result."""
        from operating.captured_originals import FAILED, REFUSED
        return REFUSED if str(handle) in self._decided else FAILED

    def _sharepoint_ahead(self, handles: Sequence[str]) -> None:
        """Read ahead what :meth:`captured` reads of each SharePoint version of
        ``handles`` (``ReadAhead`` of the capture, and the projected names), kept for this
        search call. Decides nothing: each file is still gated one by one."""
        from integrations.capture_sharepoint import ReadAhead
        store = self._sharepoint()
        if store is None:
            return
        from ontology.mentions import cited_page
        plain = [h for h in (cited_page(h)[0] for h in handles) if "#" not in h]
        memo = self._call_bound.memo
        ahead = memo.get(("sharepoint_ahead", id(store)))
        if ahead is None:
            ahead = memo[("sharepoint_ahead", id(store))] = ReadAhead(store)
        ahead.read(plain)
        self._projected_names(plain)

    def _projection_store(self) -> Any:
        """The generation this request reads: its own tools' when it serves calls, else
        the projection file as it is now. Never a whole tool table per gate: building one
        reads every audience key and BC unit of the store (1.3 GB on 2.5M objects), and
        an originals search makes a gate per original it checks."""
        return (self._projection() if self._tools is None and self._projection is not None
                else None) or self.tools.store

    def _read_ahead(self, store: Any) -> Any:
        """This search call's SharePoint read-ahead (``ReadAhead``), or None outside a
        search call."""
        bound = self._call_bound
        return bound.memo.get(("sharepoint_ahead", id(store))) if bound.search else None

    def _projected_names(self, handles: Sequence[str]) -> None:
        """What :meth:`_projected_stale` reads of each of ``handles`` (the projected
        ``row.name`` and ``row.path`` of the version), read in a few statements and kept in
        this call's memo. The same rows: a value row of a version handle belongs to an
        object holding that version."""
        if self.store_path is None:
            return
        store = self._projection_store()
        memo = self._call_bound.memo
        todo = [h for h in dict.fromkeys(handles)
                if ("projected_names", id(store), h) not in memo]
        if not todo:
            return
        with store.lock:
            for at in range(0, len(todo), 900):
                chunk = todo[at:at + 900]
                found: dict[str, list[tuple[str, Any]]] = {h: [] for h in chunk}
                for handle, path, value in store.conn.execute(
                        "SELECT l1_handle, field_path, value FROM object_values WHERE "
                        "object_id IN (SELECT object_id FROM object_versions WHERE l1_handle "
                        "IN (SELECT value FROM json_each(:h))) AND l1_handle IN (SELECT value "
                        "FROM json_each(:h)) AND field_path IN ('row.name', 'row.path')",
                        {"h": json.dumps(chunk)}):
                    found[handle].append((path, value))
                for handle, rows in found.items():
                    memo[("projected_names", id(store), handle)] = rows

    def _projected_stale(self, view: Mapping[str, Any]) -> bool:
        """Whether the projection shows this version under a name or path the capture
        no longer holds (moved since the projection was built): not served until the
        projection is rebuilt."""
        if self.store_path is None:
            return False
        held = {"row.name": view["row"].get("name"), "row.path": view["row"].get("path")}
        # The generation this request reads: its own tools' when it serves calls, else the
        # projection file as it is now. Never a whole tool table per gate: building one
        # reads every audience key and BC unit of the store (1.3 GB on 2.5M objects), and
        # an originals search makes a gate per original it checks.
        store = self._projection_store()
        kept = (self._call_bound.memo.get(("projected_names", id(store), view["l1_handle"]))
                if self._call_bound.search else None)
        if kept is not None:
            rows = kept   # read ahead with this call's other files (``_projected_names``)
        else:
            with store.lock:
                # Through the version index (object_versions by l1_handle, then
                # value_sources by object and handle): ``object_values`` alone has no
                # index on l1_handle, and this runs once per file the item gate decides.
                rows = store.conn.execute(
                    "SELECT field_path, value FROM object_values WHERE object_id IN "
                    "(SELECT object_id FROM object_versions WHERE l1_handle = ?) AND "
                    "l1_handle = ? AND field_path IN ('row.name', 'row.path')",
                    (view["l1_handle"], view["l1_handle"])).fetchall()
            if self._call_bound.search:   # this call's other gates of the same version read it once
                self._call_bound.memo[("projected_names", id(store), view["l1_handle"])] = rows
        for path, value in rows:
            if value is not None and value != held[path]:
                return True
        return False

    def _live_check(self, gated: Any, view: Mapping[str, Any]
                    ) -> tuple[str, bool, str | None] | None:
        """(permission basis, newer version at the source, why the capture decides), or
        None when a live re-read refuses the item (not visible to this reader, deleted,
        or moved since the capture). ``capture`` when the host has no live reader for
        the drive (why: None), this call's re-read bound is reached (``budget``) or the
        source failed transiently in this call (``failed:<detail>``)."""
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    RequestBudgetExhausted, SourceNotFound,
                                                    SourceUnavailable, unavailable_detail)
        drive = view["row"].get("drive_id")
        reader = (getattr(getattr(self.runtime, "live", None), "sharepoint", None)
                  or {}).get(drive)
        if self._call_bound.search:
            return ("capture", False, "search")   # a search never re-reads live
        if self._unrestricted(gated):
            # Owner-open: no live permission re-read; the captured permissions and the
            # people exclusion (already applied to this version) decide.
            return ("capture", False, None)
        if reader is None:
            return ("capture", False, None)
        key = (frozenset(gated.principals), view["l1_handle"])
        bound = self._call_bound
        if key in bound.live:
            return bound.live[key]
        if drive in bound.down:
            return ("capture", False, "failed:" + bound.down[drive])
        if not self._may_reread():
            return ("capture", False, "budget")
        bound.rereads += 1
        with self.metered() as budget:
            refused = budget.refused if budget is not None else 0
            try:
                resolved = reader.resolve_citation(view["l1_handle"], gated)
            except NotVisible as exc:
                decided = None   # refused: nothing is served
                if getattr(exc, "undecided", None) is None:
                    # Decided by the reader's permissions; a 404 (deleted) or a
                    # permission list not read in full (a transient failure) is not.
                    bound.live_decided.add(key)
            except (SourceNotFound, ReadOnlyGuardError, ValueError, LookupError):
                decided = None   # deleted or malformed: nothing is served
            except RequestBudgetExhausted:
                decided = ("capture", False, "budget")
            except SourceUnavailable as exc:
                if getattr(exc, "http_status", None) == 410:
                    decided = None   # Gone: no longer exists at that version, not transient
                else:
                    bound.down[drive] = unavailable_detail(exc) or "unclassified"
                    decided = ("capture", False, "failed:" + bound.down[drive])
            else:
                record = resolved.get("record") if isinstance(resolved, Mapping) else None
                if (not isinstance(record, Mapping)
                        or resolved.get("status") not in ("current", "changed_since")):
                    decided = None   # no current record
                elif self.runtime._employee_acl_denial(gated, "sharepoint", record):
                    decided = None   # denied for this employee
                    bound.live_decided.add(key)
                elif record.get("path") != view["row"].get("path"):
                    decided = None   # moved since the capture
                else:
                    decided = ("live", resolved["status"] != "current", None)
        if budget is not None and budget.refused > refused:
            # A request inside the re-read hit the bound (even one its reader absorbed):
            # whatever it read, e.g. part of the permissions, decides nothing.
            decided = ("capture", False, "budget")
            bound.live_decided.discard(key)
        bound.live[key] = decided
        return decided

    def _may_reread(self, gets: int = REREAD_GETS) -> bool:
        """Whether this call may start one more live read: under ``max_live_rereads``
        and, where the host's transports are known, with room for ``gets`` requests in
        this call's budget (every live request of the call counts). The budget itself
        refuses any request past it (``per_call``)."""
        bound = self._call_bound
        if bound.rereads >= bound.limit:
            return False
        return bound.budget is None or bound.budget.used + gets <= bound.budget.limit

    def may_read_live(self, handle: str) -> bool:
        """Whether a ``sharepoint://`` citation the capture does not hold may be read
        live in this call; a live read counts as a re-read against the same bounds (none
        is counted where the host has no live reader for the drive: nothing is sent)."""
        from integrations.live_source_guard import parse_handle
        try:
            (drive, _item), _version, _extra = parse_handle(handle, "sharepoint", 2)
        except ValueError:
            return True   # refused as invalid by the reader, before any request
        if drive not in (getattr(getattr(self.runtime, "live", None), "sharepoint", None)
                         or {}):
            return True
        if not self._may_reread(LIVE_READ_GETS):
            return False
        self._call_bound.rereads += 1
        return True
