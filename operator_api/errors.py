"""Refusals a person may read as they are: a stable code and a plain sentence."""

from __future__ import annotations


class UserFacing(Exception):
    def __init__(self, code: str, message: str, status: int = 503) -> None:
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


ASK_OFF = UserFacing("ask_not_served", "Ask isn't switched on on this server.", 503)
ASK_NO_AGENT = UserFacing("ask_no_agent", "Ask needs its model and tools, which this server does not run for you.", 503)
ASK_BUSY = UserFacing("busy", "The Workbench is answering several questions right now. "
                              "Try again in a minute.", 503)
ASK_TIMEOUT = UserFacing("timeout", "That question took too long to answer. Try a narrower "
                                    "question, or name the record.", 504)
BUSY = UserFacing("busy", "The Workbench is busy right now. Try again in a moment.", 503)
# The Ask's research tools did not start in time (``agent_session.ToolsUnattached``):
# nothing is answered without them.
ASK_TOOLS_STARTING = UserFacing("busy", "The Workbench is busy starting its research "
                                        "tools, so it did not answer without them. "
                                        "Try again in a minute.", 503)


class RecordNotFound(KeyError):
    """A record that is not one this reader may read: absent, withheld from them (their
    grant, markings or item gate), or not yet recorded then. The record routes answer it,
    and only it, with 404 ``not_found``; the reader cannot tell which it was. Any other
    error inside a record read (a ``KeyError`` from a bug among them) is the server's
    failure: a logged 500 with an incident id. A ``KeyError`` so the callers that treat a
    record they may not read as skipped keep doing so."""

    def __init__(self, what: str = "object_not_found") -> None:
        super().__init__(what)


class RecordAmbiguous(RecordNotFound):
    """A business reference (a bare PO or order number) that names several records of the
    type: numbers repeat across systems and counterparties (two suppliers' PO100xxx), so it
    is never one record. The record page lists them to choose from (``page_sql.choices``);
    every other record route answers it as not found (it is a ``RecordNotFound``).
    ``ids``: the records it names, before any door read (never shown as they are)."""

    def __init__(self, ids: "tuple[str, ...] | list[str] | frozenset[str]" = ()) -> None:
        super().__init__("object_ambiguous")
        self.ids = tuple(sorted(str(i) for i in ids))

    def __reduce__(self) -> tuple:
        return (RecordAmbiguous, (self.ids,))   # across the read processes, ids kept


# What ``plx.object`` answers for a record that is not one the reader may read (it answers
# one ``object_not_found`` for an absent, withheld or broken object: ``ontology_host``), a
# record a live ``plx.find`` found but the store does not hold, and one not yet captured at
# an as-of time; ``plx.around`` answers ``not_found``.
NOT_FOUND_CODES = frozenset({"object_not_found", "not_found", "not_in_projection",
                             "not_known_at"})


# What the source routes (``/api/source``, ``/api/source/records``) answer as not found
# besides a ``RecordNotFound`` (a cited source this reader may not open: absent, withheld
# or unresolvable read the same): refusals that name no particular source, so none tells
# a withheld one from a missing one. A malformed or unsupported handle, a family no host
# serves here (``citation_scheme_not_granted: ...``), no source reader on this server, a
# text offset past the source's end.
SOURCE_REFUSALS = frozenset({"l1_citation_handle_required", "citation_scheme_not_supported",
                             "source_reader_not_configured", "text_offset_out_of_bounds"})


def source_refused(error: BaseException) -> bool:
    """Whether a source read's error is "not found" for the reader (404): a
    ``RecordNotFound`` or one of ``SOURCE_REFUSALS``. Any other error, a plain
    ``KeyError`` among them (a bug inside the read), is the server's failure: a logged
    500, never told to the reader as a missing source."""
    if isinstance(error, RecordNotFound):
        return True
    if isinstance(error, KeyError) or not isinstance(error, (ValueError, LookupError)):
        return False
    text = str(error)
    return text in SOURCE_REFUSALS or text.startswith("citation_scheme_not_granted: ")


class RecordReadFailed(RuntimeError):
    """A record read the tools answered with an error that is not "not found" (the store
    unreadable, a contract broken): the server's failure, never told to the reader as a
    missing record. Carries the tool's error code only, never its message."""
