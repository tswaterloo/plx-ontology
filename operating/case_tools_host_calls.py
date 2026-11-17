"""Per-call plumbing of ``CaptureCaseHostTools``: call bounds, audience, the captured
mail and SharePoint stores, policy stamps, tool calls and read-ahead.

Split from ``operating/case_tools_host.py``; moved unchanged.
"""

from __future__ import annotations

import contextvars
from contextlib import ExitStack, contextmanager, nullcontext
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, Mapping
from ontology import mentions as mn
from operating.ontology_tools_identity import _gap
from operating.ontology_tools_evidence import skipped_gap
from operating.playbook_originals import extraction_skipped
from .case_tools_host_rules import _mail_store
from .case_tools_host_citations import (
    _ACTIVE_CALL, _AHEAD_POOL, _CallBound, _member_container,
)


class _CallsMixin:
    """Methods of ``CaptureCaseHostTools`` (``case_tools_host_calls``); mixed in there, never used alone."""

    def _new_call(self, shared: "_CallBound | None" = None) -> None:
        """Fresh per-call state; the live re-read bound is ``shared`` (the tool call in
        progress on this context, whose bound every gate built inside it draws from),
        else a new one of this host's own bounds."""
        from integrations.live_source_guard import GuardedTransport, RequestBudget
        self._served, self._refused = {}, {}
        self._over_budget, self._decided = set(), set()
        if shared is not None:
            self._bound = shared
            return
        live = getattr(self.runtime, "live", None)
        transports = [t for t in (getattr(live, "graph_transport", None),
                                  getattr(live, "bc_transport", None))
                      if isinstance(t, GuardedTransport)]
        self._bound = _CallBound(self.max_live_rereads, RequestBudget(
            transports, self.max_live_gets) if transports else None)

    @property
    def _call_bound(self) -> "_CallBound":
        """The live re-read bound this gate draws from now: its own call's while it serves
        one (shared with the tool call it was opened in, if any), else the tool call in
        progress on this context (a gate a resolve builds inside another host's call:
        ``CaseCaptures._gate``), else its own. So every gate built inside one tool call
        shares the call's one ``max_live_rereads`` and ``max_live_gets`` bound and its
        re-read answers, and a nested gate never starts a fresh budget."""
        if not self._in_call:
            active = _ACTIVE_CALL.get()
            if active is not None:
                return active
        return self._bound

    @contextmanager
    def per_call(self, *, whole_block: bool = True) -> Iterator[Any]:
        """One tool call: fresh per-call state (the mail capture's mailbox visibility
        decided once, ``capture_mail.visibility_scope``), and this call's request budget
        enforced on every live request inside the block. A call inside a call shares the outer
        call's state and budget. ``whole_block=False`` (``CaseCaptures.call``): the
        budget is enforced on this gate's own live reads only, so a case tool's other
        live reads are not refused by it, though every request of the block still
        counts against it."""
        if self._in_call:
            with self.metered() as budget:
                yield budget
            return
        from integrations.capture_mail import visibility_scope
        outer = _ACTIVE_CALL.get()
        self._new_call(outer)
        self._in_call = True
        token = _ACTIVE_CALL.set(self._bound) if outer is None else None
        try:
            with visibility_scope(), \
                    (self.metered() if whole_block else nullcontext()) as budget:
                yield budget
        finally:
            self._in_call = False
            if token is not None:
                _ACTIVE_CALL.reset(token)

    @contextmanager
    def metered(self) -> Iterator[Any]:
        """This call's request budget, enforced on the host's Graph and BC transports
        inside the block (None, and nothing enforced, when no transport is known)."""
        budget = self._call_bound.budget
        if budget is None:
            yield None
            return
        with budget.active() as active:
            yield active

    def _audience(self) -> Any:
        return getattr(getattr(self.runtime, "live", None), "audience", None)

    def _mail(self) -> tuple[Any, Any] | None:
        """(a context of the mail capture store, the live audience), or None: the case
        runtime's kept read-only store (``CaseRuntime.mail_reader``), else one opened and
        closed for this use."""
        from integrations.capture_mail import store_exists
        audience = self._audience()
        if self.mail_capture_dir is None or audience is None \
                or not store_exists(Path(self.mail_capture_dir)):
            return None
        return _mail_store(self.runtime, Path(self.mail_capture_dir)), audience

    def _sharepoint(self) -> Any:
        """The SharePoint capture store, opened read-only once it exists."""
        from integrations.capture_sharepoint import SharePointCaptureStore, store_exists
        if self._sharepoint_store is None and self.sharepoint_capture_dir is not None \
                and store_exists(self.sharepoint_capture_dir):
            self._sharepoint_store = SharePointCaptureStore.open_read_only(
                self.sharepoint_capture_dir)
        return self._sharepoint_store

    def _capture_grant_stamps(self) -> tuple[Any, Any]:
        """(mail, SharePoint): what ``policy_now`` decides from each capture store, as it
        stands now: the mail capture's mailbox rows (``_MAILBOX_ROWS``: identities, owner
        ACL, recorded resources, directory listing; every column ``audience_check``,
        ``visible_mailboxes`` and the exclusion read of a mailbox) and the SharePoint
        capture's drives (``_drives``: the only thing its audience check reads of the
        store). Not the store files' state: a capture commits every few seconds, so a kept
        policy never outlived one and every request decided it again (2-60 s of each page
        and Ask under load, 2026-11-13). A store ``policy_now`` does not open (none there,
        no live audience) grants nothing from it; one that cannot be read so is stamped
        by its file state, as before."""
        from integrations import capture_mail, capture_sharepoint
        from .ontology_host import _file_stamp
        mail = sharepoint = None
        if self.mail_capture_dir is not None:
            mail = _file_stamp(Path(self.mail_capture_dir).expanduser()
                               / capture_mail.STORE_DIR / capture_mail.DB_FILE)
            try:
                opened = self._mail()
                if opened is None:   # no store or no audience: no mail unit is granted
                    mail = ("none",)
                else:
                    with opened[0] as store:
                        cursor = store.db.execute(capture_mail._MAILBOX_ROWS)
                        names = [column[0] for column in cursor.description]
                        rows = cursor.fetchall()
                    # every column but when the identity was last checked (each capture
                    # run rewrites it; no grant reads it)
                    kept = [i for i, name in enumerate(names) if name != "identity_checked_at"]
                    mail = ("mailboxes", hashlib.sha256(json.dumps(
                        [[row[i] for i in kept] for row in rows], default=str).encode()
                    ).hexdigest())
            except Exception:  # noqa: BLE001 - unreadable: the file state, as before
                pass
        if self.sharepoint_capture_dir is not None:
            sharepoint = _file_stamp(Path(self.sharepoint_capture_dir).expanduser()
                                     / capture_sharepoint.STORE_DIR / capture_sharepoint.DB_FILE)
            try:
                store = self._sharepoint()
                if store is None:   # no store: no SharePoint unit is granted
                    sharepoint = ("none",)
                else:
                    # by the (drive_id, item_id) index, one step a drive (1 ms; the
                    # DISTINCT scan of every item took 0.03-0.75 s a request)
                    sharepoint = ("drives", tuple(row[0] for row in store.db.execute(
                        "WITH RECURSIVE d(x) AS (SELECT MIN(drive_id) FROM items UNION ALL "
                        "SELECT (SELECT MIN(drive_id) FROM items WHERE drive_id > d.x) "
                        "FROM d WHERE d.x IS NOT NULL) SELECT x FROM d WHERE x IS NOT NULL")))
            except Exception:  # noqa: BLE001 - unreadable: the file state, as before
                pass
        return mail, sharepoint

    def _exclusion_stamp(self) -> str:
        """The people exclusion's fingerprint now (never the ids): the gate results this
        host's tools share are kept per exclusion, so a person excluded since withholds at
        once (``ontology_tools_traverse``'s shared slice gate)."""
        fingerprint = getattr(getattr(self.runtime, "people_exclusion", None), "fingerprint",
                              None)
        try:
            return fingerprint() if callable(fingerprint) else "no-exclusion"
        except Exception:  # noqa: BLE001 - an unreadable guard: a key of its own
            return "unreadable"

    def _current_read(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """``captured_read`` of the item's current captured version when ``handle`` is a
        plain version handle (no page or member) the capture has superseded and this
        reader may be served the current one, a version of its own; else None."""
        from integrations import capture_sharepoint as cs
        store = self._sharepoint()
        try:
            (drive_id, item_id), _etag, extra = cs.parse_handle(handle, cs.SCHEME, 2)
            current = None if extra or store is None else store.current(drive_id, item_id)
        except Exception:  # noqa: BLE001 - no current version named
            return None
        cited = current.get("l1_handle") if isinstance(current, Mapping) else None
        if not isinstance(cited, str) or cited == handle \
                or current.get("removed_reason") is not None:
            return None
        read = self.captured_read(cited, actor)
        if not isinstance(read, Mapping) or read.get("status") != "resolved" \
                or read.get("values_citation") != cited:
            return None
        return dict(read, gaps=list(read.get("gaps") or ()))

    def _text_original(self, actor: Any, view: Mapping[str, Any]) -> str | None:
        """The ``original://`` citation of the captured original holding the text of the
        captured SharePoint version ``view`` (served to this reader) when the capture
        holds that version without usable text, and this reader may open the original
        (``CapturedOriginals.text_original_of``); else None. A container member: None."""
        from integrations import capture_sharepoint as cs
        originals = getattr(self.runtime, "captured_originals", None)
        row = view.get("row") or {}
        handle = str(view.get("l1_handle") or "")
        if originals is None or _member_container(handle) is not None:
            return None
        try:
            (drive_id, item_id), _etag, _extra = cs.parse_handle(handle, cs.SCHEME, 2)
        except ValueError:
            return None
        return originals.text_original_of(drive_id, item_id, row.get("content_sha256"),
                                          actor)

    def _refusal(self, handle: str) -> dict[str, Any] | None:
        """The ``plx.read`` refusal of a captured version ``captured`` refused in this
        call, else None."""
        refused = self._refused.get(handle)
        if refused is None:
            return None
        status, reason, detail = refused
        return {"status": status, "reason": reason,
                "gaps": [_gap("inaccessible" if status == "withheld" else "stale"
                              if reason == "capture_too_old" else "unknown",
                              reason, "sharepoint", detail)]}

    def _page_not_held(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """``plx.read`` of a page the text of a captured file this reader may be served
        (its file is served now) does not hold: past the page cap ``unresolvable`` with
        the ``skipped_by_cap`` gap, never as if the page did not exist; past the last page
        of a file whose text was read (its page count known) ``page_not_found``; else
        None (no text read: whether the page exists cannot be told). A cap whose range is
        not recorded whole takes every page not read for one past it (fail closed). The
        file's refusal is the page's: a refused reader learns nothing of its pages."""
        base, page = mn.cited_page(handle)
        view = None if page is None else self.captured(actor, base, text=True)
        if view is None:
            return None if page is None else self._refusal(base)
        skipped = extraction_skipped(view["row"])
        if skipped is not None and ("first_page" not in skipped
                                    or skipped["first_page"] <= page <= skipped["last_page"]):
            return {"status": "unresolvable", "reason": "skipped_by_cap",
                    "gaps": [*self._labels(view["l1_handle"]), skipped_gap(handle, skipped)]}
        if isinstance(view["row"].get("content_text"), str):
            return {"status": "unresolvable", "reason": "page_not_found", "gaps": []}
        return None

    @contextmanager
    def _employee_held(self, actor: Any) -> Iterator[None]:
        """The reader's employee entitlement resolved once for this tool call
        (``EmployeeActorGate.holding``): every item check of the call (a mail or file
        gate per candidate or row) reuses it instead of resolving the employee again, and
        the next call resolves anew, so a grant change applies from the next call. A
        reader the gate refuses, or a runtime with no such gate, holds nothing: each check
        resolves (and refuses) on its own, as before."""
        holding = getattr(getattr(self.runtime, "employee_gate", None), "holding", None)
        if holding is None:
            yield
            return
        with ExitStack() as stack:
            try:
                stack.enter_context(holding(actor))
            except Exception:  # noqa: BLE001 - nothing held: each check decides for itself
                pass
            yield

    def _capture_call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        if name != "plx.documents" or self.mail_capture_dir is None:
            return self._captured_call(name, arguments, actor)
        # One policy for the whole search (as the console pins one per request), so the
        # mail top-up's scan can start under it before the projected search and overlap
        # it (``mail_topup.EarlyScan``); the top-up still gates every message after. The
        # reader's SharePoint lookup is read beside it too (``_search_ahead``).
        with self.policy_pinned(actor):
            early = self._early_mail(arguments, actor)
            ahead = self._search_ahead(arguments, actor)
            return self._captured_call(name, arguments, actor, early, ahead)

    def _search_ahead(self, arguments: Any, actor: Any) -> Any:
        """The reader's SharePoint capture search (:meth:`_captured_search`: its identifier
        lookup, rebuilt after a capture commit in seconds) started now on its own thread
        and store connection, beside the projected search, or None (no capture, or not a
        documents question). Reads only: what it reads is what the call reads after the
        projected search, from the same capture; the call takes it when it needs it."""
        if self.sharepoint_capture_dir is None or not isinstance(arguments, dict) \
                or not isinstance(arguments.get("query"), str):
            return None
        from integrations.capture_sharepoint import SharePointCaptureStore, store_exists
        try:
            if self._lookup_store is None:
                if not store_exists(self.sharepoint_capture_dir):
                    return None
                self._lookup_store = SharePointCaptureStore.open_read_only(
                    self.sharepoint_capture_dir, any_thread=True)
        except Exception:  # noqa: BLE001 - the call reads it itself, as before
            return None
        context = contextvars.copy_context()
        return _AHEAD_POOL.submit(context.run, self._captured_search, arguments, actor,
                                  self._lookup_store)
