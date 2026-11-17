"""Shared bounds, gap words and small helpers of the capture case host: read outcomes,
find and re-read limits, label reserve, use-case gaps, the page-not-read refusal and
the capture-store openers.

Split from ``operating/case_tools_host.py`` (module docstring there); moved unchanged.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from operating import ontology_tools as ot
from operating.cases import CitationUnresolvable
from operating.ontology_tools_evidence import CitationReader

_READ_OK = ("resolved", "changed_since")
# plx.read's find: the places it lists (the contract's matches.hits bound) and the
# characters of context its snippet keeps on each side of the phrase.
MAX_FIND_HITS = 50
# A message's find also searches its attachments (``PagedRead._attachment_finds``): at
# most this many, and this many hits of each.
MAX_ATTACHMENT_FINDS = 12
ATTACHMENT_FIND_HITS = 5
FIND_CONTEXT_CHARS = 80
PAGE_NOT_READ = "page_not_read"
_METHOD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]{0,63}$")
_WORD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{5,18}(?:Z|[+-][0-9]{2}:[0-9]{2})$")


class _Recording(CitationReader):
    """A ``CitationReader`` that keeps the last raw read, so a page is cut from exactly
    the content the generic read decided on (one read, one digest)."""

    def __init__(self, reader: CitationReader,
                 mark: Callable[[str, Mapping[str, Any], Any], dict[str, Any]] | None = None
                 ) -> None:
        self.reader = reader
        self.mark = mark
        self.last: dict[str, Any] | None = None

    @property
    def runtime(self) -> Any:
        """The wrapped reader's case runtime (its ``state_dir`` names its money profile)."""
        return getattr(self.reader, "runtime", None)

    def state_dirs(self) -> tuple[str, ...]:
        from operating.ontology_tools_evidence import reader_state_dirs
        return reader_state_dirs(self.reader)

    def read(self, handle: str, actor: Any, *, page: int | None = None) -> dict[str, Any]:
        """The reader's read; ``last`` keeps it as the reader may open it (``mark``: the
        value markings of a raw row, ``EvidenceTools._marked``), so no page, window,
        find or column page cut from it shows a value the read withholds."""
        got = self.reader.read(handle, actor, page=page)
        self.last = self.mark(handle, got, actor) if self.mark is not None else got
        return got

    def originals(self, actor: Any, limit: int) -> list[dict[str, Any]]:
        return self.reader.originals(actor, limit)

    def original_text(self, handle: str, actor: Any) -> dict[str, Any] | None:
        return self.reader.original_text(handle, actor)

    def field_text(self, handle: str, field_path: str, actor: Any) -> str | None:
        return self.reader.field_text(handle, field_path, actor)


def _output_properties(contracts: Mapping[str, Any], output: Mapping[str, Any]
                       ) -> dict[str, Any]:
    """The properties a tool's output may carry: its own, or for a ``oneOf`` those of its
    alternatives (``$defs`` references followed), the first alternative naming a property
    deciding it. The first alternative alone is a ``$ref`` for a tool whose answers are
    shaped by name (``plx.aggregate``): reading only its own properties found no ``gaps``
    and left a count over captured files unlabelled."""
    defs = contracts.get("$defs") or {}
    merged: dict[str, Any] = {}
    for alternative in output.get("oneOf") or [output]:
        ref = alternative.get("$ref") if isinstance(alternative, Mapping) else None
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            alternative = defs.get(ref[len("#/$defs/"):], {})
        for name, spec in ((alternative or {}).get("properties") or {}).items():
            merged.setdefault(name, spec)
    return merged


def _text_of(value: Any) -> str | None:
    """A field as the text a window pages: a string, or JSON for an object or array."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return None


_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?$")
_ROW_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Unsearched captured files (text not searched) gated to be counted per plx.documents call;
# past it the counts are lower bounds (``_captured_documents``).
UNSEARCHED_GATE = 100
# A query word a captured file's name or folder path must hold to be listed by name
# (``_name_terms``): letters or digits, this long at least.
NAME_TERM_MIN = 2
_NAME_TOKEN = re.compile(r"[^\W_]+")
_NAME_PART = re.compile(r"[0-9]+|[^\W\d_]+")
# Graph GETs one live re-read may send: the item's metadata plus at most three pages of
# its permissions (integrations.graph_read.SharePointReader._permissions).
REREAD_GETS = 4
# Graph GETs one live read of a citation the capture does not hold may send: the record
# read, the current version's read when it changed, and the digest read
# (RuntimeCitations._read_live), each one re-read.
LIVE_READ_GETS = 3 * REREAD_GETS
# Default per-call bounds on live re-reads and on the Graph GETs they send.
MAX_LIVE_REREADS = 50
MAX_LIVE_GETS = 200
# Room plx.investigate's pages leave for the capture labels (``_label``): the summary
# label (its detail capped at 600 characters) and the SharePoint freshness entry, each
# counted twice in the envelope, always fit within it.
LABEL_RESERVE_BYTES = 2048


# The search tools: their results are never re-read live (``_CallBound.search``), and a
# query naming a use case gets that use case's documents (``_use_case_documents``).
SEARCH_TOOLS = frozenset({"plx.documents", "plx.find"})
# The gaps a use-case block brings (:meth:`CaptureCaseHostTools._use_case_documents`):
# kept over every other gap when an answer is trimmed to fit.
USE_CASE_GAPS = frozenset({"use_case_documents_withheld", "use_case_documents_not_searched",
                           "use_case_document_not_held", "unknown_use_case",
                           "use_case_catalog_unreadable"})
USE_CASE_NOTE = ("Answer a question about this use case from these documents, never from "
                 "another use case's map; read a cited map whole (page with text_offset "
                 "until complete) before saying something is absent from it.")


# A handle whose message ``_mail_facts`` could not name (read by the full row instead).
_UNREAD = object()

# Statuses of a permission list the capture read (``LiveSourceAudience.visible_all``).
_RECORDED_ACL = ("read", "derived")


# ``_captured_documents``' search not read earlier in its call.
_UNPREPARED = object()


class PageNotRead(CitationUnresolvable):
    """A citation of a page of a captured file this reader may be served that its text
    reading never read: past the page cap (``skipped_by_cap``) or past its last page.
    Public, as ``case_runtime.LiveSourceNotConfigured`` is: raised only once the file
    itself is served to the reader, it tells nothing the file's own read does not."""

    code = PAGE_NOT_READ
    public = True

    def __init__(self) -> None:
        super().__init__(PAGE_NOT_READ)


def _close_reader(reader: ot.StoreReader) -> None:
    """Close ``reader``'s connection once no gate's query is running on it (its lock)."""
    with reader.lock:
        reader.conn.close()


def _mail_store(runtime: Any, directory: Path) -> Any:
    """A context of the mail capture store under ``directory``: the runtime's kept
    read-only one (``CaseRuntime.mail_reader``), else one opened and closed for this use."""
    from integrations.capture_mail import MailCaptureStore
    reader = getattr(runtime, "mail_reader", None)
    if callable(reader):
        return reader(directory)
    return contextlib.closing(MailCaptureStore(directory))


def _current_version(store: Any, handle: str) -> bool:
    """Whether the SharePoint capture holds ``handle`` (a plain version handle, no page,
    pin or member) as its item's current, not removed, version: a reader refused it was
    refused a version that exists. Any doubt: False."""
    from integrations import capture_sharepoint as cs
    try:
        (drive_id, item_id), _etag, extra = cs.parse_handle(handle, cs.SCHEME, 2)
        if extra:
            return False
        current = store.current(drive_id, item_id)
    except Exception:  # noqa: BLE001 - undecided: not a refusal
        return False
    return (isinstance(current, Mapping) and current.get("l1_handle") == handle
            and current.get("removed_reason") is None)
