"""Per-call bounds and captured citations of the capture case host: the call bound,
the memoized page build and ``CapturedCitations``.

Split from ``operating/case_tools_host.py``; moved unchanged.
"""

from __future__ import annotations

import concurrent.futures
from contextlib import contextmanager
from contextvars import ContextVar
import os
from typing import Any, Callable, Iterator, Mapping, Sequence
from operating.ontology_tools_identity import _gap
from operating.ontology_tools_evidence import summarise_mentions, RuntimeCitations


class _CallBound:
    """The live re-read bound of one tool call, shared by every SharePoint gate built
    inside it (``CaptureCaseHostTools._call_bound``): the re-reads and live reads started
    against ``limit``, the request budget over the host's transports (None when none is
    known), and the re-read answers (per reader principals and version), the reads a
    reader's permissions decided and the drives whose source failed in the call."""

    def __init__(self, limit: int, budget: Any) -> None:
        self.limit, self.budget, self.rereads = limit, budget, 0
        self.live: dict[tuple, Any] = {}
        self.live_decided: set[tuple] = set()
        self.down: dict[str, str] = {}
        # True while a search tool runs (SEARCH_TOOLS): no live re-read is started.
        self.search = False
        # Per-call decisions of the search mail gate (``_mail_search_gate``).
        self.memo: dict[Any, Any] = {}


# Reads a documents search's SharePoint lookup beside its projected search
# (``CaptureCaseHostTools._search_ahead``).
# One per core: two served every reader's search, so a third concurrent search waited for
# both before its own lookup started. One that has not started when the projected search
# ends is cancelled and read on the caller's thread (``_captured_call``).
_AHEAD_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(2, min(16, os.cpu_count() or 2)), thread_name_prefix="plx-sp-lookup")


# The bound of the tool call in progress on this context (``CaptureCaseHostTools.per_call``).
# How many of a plx.documents page's documents are the search's own (its cursor pages
# them): the captured SharePoint files :meth:`CaptureCaseHostTools._captured_documents`
# appends after them are on no page. None: all of them.
_SEARCH_HITS: ContextVar[int | None] = ContextVar("plx_capture_search_hits", default=None)
_ACTIVE_CALL: ContextVar[_CallBound | None] = ContextVar("plx_capture_call_bound",
                                                         default=None)
# True while the app builds a page (Home, the briefing, a record page and its sections, a
# kept page's re-check: ``page_build``): every mail item gate of each tool call in it is the
# per-call memoized one (``_mail_search_gate``), as a search's is.
_PAGE_BUILD: ContextVar[bool] = ContextVar("plx_page_build", default=False)


@contextmanager
def page_build() -> Iterator[None]:
    """A page build on this context (``_PAGE_BUILD``): its mail item gates decide once per
    tool call on the capture's rows, with the same decisions as the full check."""
    token = _PAGE_BUILD.set(True)
    try:
        yield
    finally:
        _PAGE_BUILD.reset(token)


class CapturedCitations(RuntimeCitations):
    """``RuntimeCitations`` that reads a ``sharepoint://`` citation from the SharePoint
    capture (``CaptureCaseHostTools.captured_read``): the captured file, or the refusal
    of a captured file the host's gate refuses (never read live in its place). A
    SharePoint file the capture does not hold for this reader is read live within the
    host's per-call bounds (``CaptureCaseHostTools.may_read_live``); anything else reads
    as before, except that a read a request refusal of this call's bound cut off is a
    ``live_read_budget`` refusal (nothing of it served)."""

    def __init__(self, runtime: Any, host: "CaptureCaseHostTools", **options: Any) -> None:
        super().__init__(runtime, **options)
        self.host = host

    def read(self, handle: str, actor: Any, *, page: int | None = None) -> dict[str, Any]:
        if str(handle).startswith("sharepoint://"):
            paged = handle if page is None else _with_page(handle, page)
            captured = None if paged is None else self.host.captured_read(paged, actor)
            if captured is not None:
                return captured
            if page is None and not self.host.may_read_live(handle):
                return self._budget_refusal(handle, "the file was not read")
        with self.host.metered() as budget:
            refused = budget.refused if budget is not None else 0
            result = super().read(handle, actor, page=page)
        if budget is not None and budget.refused > refused:
            # A request inside the read hit the bound: whatever it read is partial.
            return self._budget_refusal(handle, "the read was cut off and nothing of it "
                                        "is served")
        return result

    def _budget_refusal(self, handle: str, what: str) -> dict[str, Any]:
        source = str(handle).split("://", 1)[0][:40]
        return self._out("unresolvable", "live_read_budget", gaps=[_gap(
            "unknown", "live_read_budget", source,
            f"this call's live read bound is reached; {what}; ask again in a new call")])

    def field_text(self, handle: str, field_path: str, actor: Any) -> str | None:
        if str(handle).startswith("sharepoint://") and field_path == "row.content_text":
            view = self.host.captured(actor, handle, text=True)
            text = None if view is None else view["row"].get("content_text")
            return text if isinstance(text, str) else None
        return super().field_text(handle, field_path, actor)


def _carried_withheld(citations: Any, stores: Callable[[], Any], handle: str,
                      memo: dict) -> bool:
    """Whether a mail attachment citation (or a member, or a page of it) is withheld
    from every reader for the mail it carries (DOC-008:
    ``MailCaptureStore.carried_withholds`` with the runtime's people guard), decided once
    per call (``memo``); any failure withholds it."""
    if "#" not in handle:
        return False
    key = ("carried", handle)
    if key not in memo:
        try:
            with stores() as store:
                memo[key] = store.carried_withholds(handle, citations._people())
        except Exception:  # noqa: BLE001 - not decided: withheld
            memo[key] = True
    return memo[key]


def _member_container(handle: str) -> str | None:
    """The container handle of a container member citation (with or without a page;
    ``integrations.captured_text.member_handle``), else None."""
    from integrations.captured_text import split_member
    from ontology.mentions import cited_page

    member = split_member(cited_page(handle)[0])
    return None if member is None else member[0]


def _with_page(handle: str, page: int) -> str | None:
    """``handle`` narrowed to one page (``#page=<n>``), or None when it already names
    another page."""
    from integrations.live_source_guard import parse_handle
    try:
        _parts, _version, extra = parse_handle(handle, "sharepoint", 2)
    except ValueError:
        return None
    if "page" in extra:
        return handle if extra["page"] == str(page) else None
    return f"{handle}{'&' if '#' in handle else '#'}page={page}"


def _file_priors(query: str) -> Callable[[Mapping[str, Any]], float]:
    """A listed document's cheap prior (``operating.search.authority``): a use-case
    catalog document, a captured original, a title or file name naming an identifier the
    query names. It orders the captured files before their gate and places them among
    the projection's page; weights that cannot be read give every document 0 (the
    store order stays)."""
    from operating.search import authority as au
    try:
        rule = au.load()
    except Exception:  # noqa: BLE001 - no weights: no prior, the store order stays
        return lambda _document: 0.0
    catalog, wanted = au.catalog_keys(), au.query_identifiers(query)

    def prior(document: Mapping[str, Any]) -> float:
        signals = []
        key = au.document_key(document.get("citation"))
        if key is not None and key in catalog:
            signals.append("use_case_document")
        if document.get("kind") == "original":
            signals.append("captured_original")
        title = document.get("title")
        if au.names_identifier(title.get("untrusted_text") if isinstance(title, Mapping)
                               else title, wanted):
            signals.append("identifier_in_title")
        return rule.prior(signals)
    return prior


def _placed(documents: Sequence[dict[str, Any]], added: Sequence[dict[str, Any]],
            prior: Callable[[Mapping[str, Any]], float]) -> list[dict[str, Any]]:
    """The projection's page with the captured files ``added`` (best first) placed in it:
    each before the first listed document whose cheap prior is lower, or equal and
    dated earlier (newest first among equals), else after them. The projection's own
    order is kept."""
    out = list(documents)
    at = 0
    for document in added:
        mine, date = prior(document), str(document.get("date") or "")
        while at < len(out) and (prior(out[at]) > mine or (
                prior(out[at]) == mine and str(out[at].get("date") or "") >= date)):
            at += 1
        out.insert(at, document)
        at += 1
    return out


def _with_mentions(document: Mapping[str, Any], mentions: list[dict[str, Any]],
                   whole: Sequence[Mapping[str, Any]] | None = None, offset: int = 0
                   ) -> dict[str, Any] | None:
    """``document`` with the captured-text ``mentions`` it does not already cite (same
    identifier at the same cited page), before its own (a page-cited span no projected
    mention gives), summarised again
    (``summarise_mentions`` from ``offset``); ``whole``: all of its own mentions (the
    store's list before it was summarised, ``full_mentions_kept``), else the ones it
    shows. None when none is new."""
    def key(mention: Mapping[str, Any]) -> tuple[Any, ...]:
        origin = mention["span"]["origin"]
        return (mention["identifier_kind"], mention["normalized"], origin["citation"],
                origin.get("field_path"))

    have = list(whole if whole is not None else document.get("mentions") or ())
    known = {key(m) for m in have}
    new = [m for m in mentions if key(m) not in known]
    if not new:
        return None
    merged = summarise_mentions(dict(document, mentions=[*new, *have]), offset)
    return dict(merged, snippet=document.get("snippet") or new[0]["span"])
