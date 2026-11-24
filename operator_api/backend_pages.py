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


class AccessState(tuple):
    """A reader's access state (``WorkspaceBackend._access_job``) with ``legacy``: the same
    state with the document-marks part as code before 11-22 kept it (``_marks_stamp`` with
    the PLX-issued documents' digest), or None when that is the same. A page kept under
    that older form is the reader's while the withheld set and the PLX-issued set are both
    the ones it was read under (``same_access``), so a deploy that moved the part's format
    refuses no kept page (11-22: every record page was refused and read again cold)."""

    legacy: Optional[tuple] = None

    def __reduce__(self) -> tuple:
        # kept on disk as a plain tuple (any code version reads it)
        return (tuple, (tuple(self),))


# The policy parts (``permissions.policy_parts``) that also describe other readers: the whole
# audience map (every pattern and who holds it) and its pattern names. A new mailbox or meeting
# key of someone else moved both on each delta, so a non-full reader's kept pages were refused
# after every deploy (11-22 live: 89 of 296 refusals, policy parts ["audiences",
# "audiences_keys"] alone). They are left out only beside ``audiences_reach``: every entry that
# can decide a key one of the reader's own patterns matches (a deny or another person's entry
# inside a held wildcard moves it), compared with ``audiences_held``.
OTHERS_PARTS = frozenset({"audiences", "audiences_keys"})
REACH_PART = "audiences_reach"


def _parts_of(state: Any) -> Optional[dict]:
    if not isinstance(state, tuple) or not state or not isinstance(state[0], tuple):
        return None
    if not all(isinstance(p, tuple) and len(p) == 2 for p in state[0]):
        return None
    return dict(state[0])


def _without(state: tuple, names: frozenset) -> tuple:
    return (tuple(p for p in state[0] if p[0] not in names),) + tuple(state[1:])


def _reader_match(now: Any, held: Any) -> bool:
    """Whether two access states are the same for this reader: equal with the parts of
    other readers left out when both hold the reader's own reach of the audience map
    (``REACH_PART``) and its held patterns; a state kept before that part existed is
    compared whole, the part left out of the newer one."""
    a, b = _parts_of(now), _parts_of(held)
    if a is None or b is None:
        return now == held
    if REACH_PART in a and REACH_PART in b and "audiences_held" in a \
            and "audiences_held" in b:
        return _without(now, OTHERS_PARTS) == _without(held, OTHERS_PARTS)
    reach = frozenset({REACH_PART})
    return _without(now, reach) == _without(held, reach)


EXCLUSION_PART = 2   # (the exclusion's rule stamp, the precomputed set's version or None)


def _set_version(state: Any) -> tuple[bool, Any]:
    """(whether ``state`` holds the exclusion part as (rule, set version), that version)."""
    part = state[EXCLUSION_PART] if isinstance(state, tuple) \
        and len(state) > EXCLUSION_PART else None
    if isinstance(part, tuple) and len(part) == 2:
        return True, part[1]
    return False, None


def _rule_only(state: Any) -> Any:
    """``state`` with the exclusion set's version left out of its exclusion part (the rule
    stays); any other shape as it is."""
    if not _set_version(state)[0]:
        return state
    return tuple(state[:EXCLUSION_PART]) + ((state[EXCLUSION_PART][0], None),) \
        + tuple(state[EXCLUSION_PART + 1:])


def exclusion_set_moved(held: Any) -> bool:
    """Whether a kept page's access state ``held`` was read under another version of the
    precomputed exclusion set than this process holds now (``permissions.
    exclusion_set_version``). Only a full reader's state holds one (staff: None, their
    gates decide). Such a page is the reader's only once every record it shows is
    decided again (``_held_good``): the set gains rows for newer versions only, so the
    version moved says nothing about what the page shows until that is checked."""
    from operating.pg.permissions import exclusion_set_version
    shaped, version = _set_version(held)
    if not shaped or version is None:
        return False
    try:
        now = exclusion_set_version()
    except Exception:  # noqa: BLE001 - undecidable: treated as moved (checked again)
        return True
    return now != version


def same_access(now: Any, held: Any) -> bool:
    """Whether a kept page's access state ``held`` is the reader's state ``now``: equal, or
    equal to ``now``'s older form (``AccessState.legacy``), each as ``_reader_match``
    compares them (the parts that only describe other readers left out), the exclusion
    set's version left out (``exclusion_set_moved``: a page under another one is served
    only after its records are decided again)."""
    if now == held:
        return True
    legacy = getattr(now, "legacy", None)
    now, held = _rule_only(now), _rule_only(held)
    if now == held or _reader_match(now, held):
        return True
    return legacy is not None and _reader_match(_rule_only(legacy), held)


def same_answer_access(now: Any, held: Any) -> bool:
    """``same_access`` for an answer kept with (access state, the PLX-issued documents'
    digest) (``InsightsMixin._insight_access``): equal, or ``held`` the state alone as
    code before 11-22 kept it (its marks part then held that digest: ``same_access``)."""
    if now == held:
        return True
    # an answer names no records to decide again: the exclusion set's version must be
    # the one it was read under (``same_access`` alone leaves it to ``_held_good``)
    return isinstance(now, tuple) and len(now) == 2 and same_access(now[0], held) \
        and _set_version(now[0]) == _set_version(held)


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
