"""A record page's core read: the record's own card, served before anything else.

The page's quick read walks the record's links, timeline and documents and checks a
customer's orders (tens of seconds cold, under load); its core read is the record's card
alone (one ``plx.object``), so the page shows the record's details at once while the
rest streams in. It is read as the reader, with the same resolution and visibility as the
page (``page_oid``: one the reader cannot see is not found), and kept per reader under
the page's own key (``page_key``: the reader's policy, the generation and the markings),
so no reader is ever served another's copy.
"""

from __future__ import annotations

from typing import Any

from . import models as m
from .errors import UserFacing

# On the read worker's queue ahead of page and section reads: it is the first thing the
# reader sees and costs one card read.
CORE_PRIORITY = -1


def core_page(backend: Any, view: Any, object_type: str, object_id: str) -> m.ObjectPage:
    """The record's core page for this reader, kept (``read_core``); when this reader's
    whole page is already kept (every section merged), that page itself (``core`` false,
    ``complete`` as kept), so the app asks for no section. RecordNotFound when the route
    names nothing this reader may see."""
    from .backend import full_page, page_key, page_oid
    from .page_parts import page_handles

    with view._request():
        oid = page_oid(backend, view, object_type, object_id)
        whole = page_key(view, oid)
        if whole is not None and backend.page_cache.get(whole) is not None:
            return backend.page_cache.kept(view, whole, lambda: full_page(backend, view, oid),
                                           page_handles,
                                           copy=lambda model: model.model_copy(deep=True))
        key = page_key(view, ("core", oid))
    return backend.page_cache.kept(view, key, lambda: read_core(backend, view, oid),
                                   page_handles,
                                   copy=lambda model: model.model_copy(deep=True))


def read_core(backend: Any, view: Any, oid: str) -> m.ObjectPage:
    """The card's title and facts, each citing a source this reader may open; nothing the
    walk decides (status, timeline, links, documents, what was not found, conflicts) is
    said: the status is "reading" until the quick read replaces the page."""
    from . import presentation
    from .backend import card_facts
    from .page_parts import page_handles, raw_type_label

    page = view.card(oid)   # its own request; RecordNotFound when not visible
    if page.get("status") != "card":
        # the card could not be read: no core page (the quick read says why)
        raise CoreUnread(oid)
    kind = str(((page.get("card") or {}).get("object") or {}).get("object_type") or "Object")
    with view._request():
        label, facts, _values, _currency = card_facts(backend, view, page, core=True)
        type_label = raw_type_label(kind)
        model = m.ObjectPage(
            object_id=oid, type=kind, type_label=type_label, label=label[:200],
            summary=type_label or kind, status=m.ObjectStatus(state="reading", text=""),
            facts=facts, complete=False, core=True,
            freshness=backend.freshness(page, handles=page_handles(facts)))
    return presentation.polished(model)


class CoreUnread(UserFacing):
    """The record's card returned an error: no core page; the page's quick read is the
    answer (the app keeps waiting for it)."""

    def __init__(self, oid: str) -> None:
        super().__init__("core_unread", "This record's details are still being read.", 503)


class CorePageMixin:
    """``WorkspaceBackend.object_core``."""

    def object_core(self, reader: Any, object_type: str, object_id: str) -> m.ObjectPage:
        """The record's core page (``core_page``) on the read worker, ahead of the page's
        other reads; one served under a full-read grant is labelled and journalled."""
        from .backend import PAGE_TIMEOUT_SECONDS
        page = self.reads.run(reader, lambda view: core_page(self, view, object_type,
                                                             object_id),
                              timeout=PAGE_TIMEOUT_SECONDS, priority=CORE_PRIORITY)
        return self._labelled(reader, page, "/api/object")
