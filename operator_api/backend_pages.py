"""Record pages and their sections: keys, kept copies, models and reads."""

from __future__ import annotations

import logging
import contextvars
import hashlib
import json
from typing import Any, Iterable, Optional

from . import models as m


_STAMP_SEEN: dict[str, str] = {}


def _note_stamp(part: str, value: str) -> None:
    """Say when a part of every reader's marks stamp moves in this process (each move
    refuses the pages kept before it): the part and a digest prefix, never an id."""
    last = _STAMP_SEEN.get(part)
    if last != value:
        _STAMP_SEEN[part] = value
        # the first value too: a page another process kept is refused when its differs
        logging.getLogger("operator_api.kept").info(
            "marks stamp part %s %s", part,
            f"is {value}" if last is None else f"moved: {last} -> {value}")


def _marks_stamp(view: Any, own_issued: bool = True) -> Optional[str]:
    """A cheap fingerprint of the documents marked beyond this reader
    (``document_marks.withheld_set``), so a re-mark is a new page key. None when no marking
    applies; a store that cannot be read is its own value, so the page is rebuilt when it
    can. ``own_issued`` False: the withheld set alone, without the PLX-issued documents
    every reader is kept from (a kept main screen is then refused only when it shows one:
    ``own_shown``)."""
    from operating.search import document_marks
    try:
        grant = getattr(view.access, "grant", None)
        held = frozenset(grant()[1]) if callable(grant) else frozenset()
    except Exception:  # noqa: BLE001 - undecidable: nothing held (fail closed)
        held = frozenset()
    got = document_marks.withheld_set(held)
    _note_stamp("marks", "unreadable" if got is None else "read")
    if got is None:
        return "unreadable"
    # the PLX-issued documents every reader is kept from (``operating.pg.own_issued``):
    # a page kept before they were known (or before a refresh added one) is rebuilt
    own = _own_stamp(document_marks._own_issued()) if own_issued else ""
    if own_issued:
        _note_stamp("own", own[:12] or "none")
    if not got[0] and not got[1] and not own:
        return None
    # one digest per (withheld set, PLX-issued stamp): the withheld set is the same object
    # while the loaded marks are (``document_marks``: kept per reader marking set), and
    # sorting and hashing a staff reader's thousands of ids on every access check cost
    # ~11 s of CPU in one 3-visit walk (11-18 profile)
    hit = _MARK_DIGESTS.get(id(got))
    if hit is not None and hit[0] is got and hit[1] == own:
        return hit[2]
    digest = hashlib.sha256(json.dumps([sorted(got[0]), sorted(got[1])]
                                       + ([own] if own else [])).encode()).hexdigest()[:16]
    if len(_MARK_DIGESTS) >= 64:
        _MARK_DIGESTS.clear()
    _MARK_DIGESTS[id(got)] = (got, own, digest)
    return digest


# id(withheld set) -> (that set, the PLX-issued stamp, its digest) (``_marks_stamp``)
_MARK_DIGESTS: dict[int, tuple] = {}


_OWN_STAMPS: dict[frozenset, str] = {}
_OWN_LAST: tuple = (None, "")   # (the set asked last (itself), its digest), swapped whole


def _own_stamp(own: frozenset) -> str:
    """A digest of the PLX-issued ids (one per loaded set: a page asks on every read).
    The set asked last is found by identity first: a background refresh loads an equal
    set as a new object, and a lookup of it compared all ~38k ids with the kept key's on
    every access check (2 s of the interpreter lock per Home visit, 11-19 profile)."""
    if not own:
        return ""
    global _OWN_LAST
    last = _OWN_LAST
    if last[0] is own:
        return last[1]
    hit = _OWN_STAMPS.get(own)
    if hit is None:
        if len(_OWN_STAMPS) >= 4:
            _OWN_STAMPS.clear()
        hit = _OWN_STAMPS[own] = hashlib.sha256(
            "\n".join(sorted(own)).encode()).hexdigest()[:16]
    _OWN_LAST = (own, hit)
    return hit


def own_issued_stamp() -> Optional[str]:
    """The PLX-issued documents' digest now (``_own_stamp``); None when unreadable. For an
    answer whose rows cannot be named one by one (an Insights tile, a search answer):
    kept under it, so a new PLX-issued document reads it again."""
    from operating.search import document_marks
    try:
        return _own_stamp(document_marks._own_issued())
    except Exception:  # noqa: BLE001 - undecidable: nothing kept matches
        return None


# the PLX-issued set's digest -> when this process first saw it (wall clock); bounded
_OWN_SEEN: dict[str, float] = {}
# a page read up to this long before the set it is judged against was first seen may hold
# a document the set names (its read ran across the change): refused too
OWN_READ_MARGIN_SECONDS = 300.0


def own_shown(objects: Optional[Iterable[str]], read_at: Optional[float]) -> bool:
    """Whether a kept page may show a PLX-issued document (``operating.pg.own_issued``,
    kept from every reader) it must no longer show: one of the records it shows
    (``objects``: ``shown_objects``) is in the set now; or, for a page whose records cannot
    all be named (None), the set it is judged against was first seen by this process
    after (or within ``OWN_READ_MARGIN_SECONDS`` before) it was read (``read_at``, wall
    clock; None: refused). The set unreadable: refused. A new PLX-issued document so
    refuses only the pages that show it, instead of every page of every reader (11-22:
    ~2,000 kept pages refused in 6 h over ~10 moves of the set)."""
    from operating.search import document_marks
    import time as _time
    try:
        own = document_marks._own_issued()
    except Exception:  # noqa: BLE001 - undecidable: refused
        return True
    if not isinstance(own, frozenset):
        return True
    if objects is not None:
        return any(o in own for o in objects)
    digest = _own_stamp(own)
    seen = _OWN_SEEN.get(digest)
    if seen is None:
        if len(_OWN_SEEN) >= 16:
            _OWN_SEEN.clear()
        seen = _OWN_SEEN[digest] = _time.time()
    return read_at is None or seen > read_at - OWN_READ_MARGIN_SECONDS


class _ReadByOtherCode(tuple):
    """A last good page (when read, the page, the access state) read by another code
    version (``_held_good``), with ``objects``: every record id it shows, each decided for
    the reader again before it is served."""

    objects: frozenset[str]

    def __new__(cls, held: tuple, objects: Iterable[str]) -> "_ReadByOtherCode":
        out = super().__new__(cls, held)
        out.objects = frozenset(objects)
        return out


def _portable_model(value: Any) -> tuple[str, str, Any]:
    """A screen's page as data any code version reads back (``_kept_good_model``): its
    model's name in ``operator_api.models`` and its JSON form."""
    return ("model", type(value).__name__, value.model_dump(mode="json"))


def _kept_good_model(value: Any) -> Any:
    """A last good page as kept on disk (``_portable_model`` in place of the model, the
    code version it was read under last), built again with the code serving it: None when
    it no longer fits that code's model (a field added or changed by a deploy) or is not of
    that shape."""
    if not (isinstance(value, tuple) and len(value) == 4 and isinstance(value[1], tuple)
            and len(value[1]) == 3 and value[1][0] == "model"):
        return None
    model = getattr(m, str(value[1][1]), None)
    from pydantic import BaseModel
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return None
    try:
        return (value[0], model.model_validate(value[1][2]), value[2], value[3])
    except ValueError:   # pydantic's ValidationError: the page no longer fits
        return None


# How a record page's margin reads its orders side by side (``WorkspaceBackend._spread``:
# the Ask pool's workers, each as this reader); None: one after another on the page's view.
SPREAD_SECTIONS = ("margin",)   # read beside the Ask pool rather than on a section slot


PAGE_SPREAD_PRIORITY = 2   # after a question waiting on the Ask pool (priority 1)


PAGE_SPREAD: "contextvars.ContextVar[Optional[Callable[..., list]]]" = contextvars.ContextVar(
    "plx_page_spread", default=None)


def plan_families(kind: str) -> set[str]:
    from operator_api.workspace.object_workspace import plans_for
    return {plan["family"] for plan in plans_for(kind)} if kind else set()


def _count(value: Any) -> int:
    """A non-negative count from a mapping value (anything else is 0)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
