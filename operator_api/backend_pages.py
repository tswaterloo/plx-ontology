"""Record pages and their sections: keys, kept copies, models and reads."""

from __future__ import annotations

import contextvars
import hashlib
import json
from typing import Any, Iterable, Optional

from . import models as m


def _marks_stamp(view: Any) -> Optional[str]:
    """A cheap fingerprint of the documents marked beyond this reader
    (``document_marks.withheld_set``), so a re-mark is a new page key. None when no marking
    applies; a store that cannot be read is its own value, so the page is rebuilt when it
    can."""
    from operating.search import document_marks
    try:
        grant = getattr(view.access, "grant", None)
        held = frozenset(grant()[1]) if callable(grant) else frozenset()
    except Exception:  # noqa: BLE001 - undecidable: nothing held (fail closed)
        held = frozenset()
    got = document_marks.withheld_set(held)
    if got is None:
        return "unreadable"
    # the PLX-issued documents every reader is kept from (``operating.pg.own_issued``):
    # a page kept before they were known (or before a refresh added one) is rebuilt
    own = _own_stamp(document_marks._own_issued())
    if not got[0] and not got[1] and not own:
        return None
    return hashlib.sha256(json.dumps([sorted(got[0]), sorted(got[1])]
                                     + ([own] if own else [])).encode()
                          ).hexdigest()[:16]


_OWN_STAMPS: dict[frozenset, str] = {}


def _own_stamp(own: frozenset) -> str:
    """A digest of the PLX-issued ids (one per loaded set: a page asks on every read)."""
    if not own:
        return ""
    hit = _OWN_STAMPS.get(own)
    if hit is None:
        if len(_OWN_STAMPS) >= 4:
            _OWN_STAMPS.clear()
        hit = _OWN_STAMPS[own] = hashlib.sha256(
            "\n".join(sorted(own)).encode()).hexdigest()[:16]
    return hit


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
