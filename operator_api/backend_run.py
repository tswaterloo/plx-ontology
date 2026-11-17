"""The workspace backend's worker processes and their entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import models as m
from .answers import normalize


def directory_oids(resolver: Any) -> list[str]:
    """The Entra object ids of the Portal snapshot's active users of the resolver's tenant
    (as ``view_as_for`` lists them), sorted; none without a Portal input."""
    rows = getattr(getattr(resolver, "_portal", None), "_rows", None)
    if not callable(rows):
        return []
    return sorted({str(r.entra_object_id).strip().lower() for r in rows()
                   if r.is_active and r.entra_object_id
                   and r.entra_tenant_id == resolver.policy.entra_tenant_id})


def view_as_for(resolver: Any, state_dir: Any) -> Any:
    """The owner's "view as" over the served resolver (``operator_api.view_as``): the
    candidates are the Portal snapshot's active users with an Entra object id, each viewed
    with ``session_identity.view_as_session``; the people exclusion fails closed. A resolver
    whose Portal input lists nobody (no snapshot) offers nobody."""
    from operating import briefs as br
    from operating.session_identity import view_as_session
    from . import view_as as va

    def candidates() -> list[tuple[str, str]]:
        rows = getattr(getattr(resolver, "_portal", None), "_rows", None)
        if not callable(rows):
            return []
        return [(r.entra_object_id, r.email) for r in rows()
                if r.is_active and r.entra_object_id
                and r.entra_tenant_id == resolver.policy.entra_tenant_id]

    def excluded() -> Callable[[str], bool]:
        people = br.env_people()
        if people is None or getattr(people, "unavailable", "absent"):
            raise RuntimeError("people exclusion unavailable")
        return lambda oid: bool(people.is_excluded(oid))

    return va.ViewAs(candidates=candidates, excluded=excluded,
                     identity=lambda oid, exp: view_as_session(resolver, oid, expires_at=exp),
                     audit=va.Audit(Path(state_dir) if state_dir else None))


def _keep_found(got: m.UnderstandingEvent, base: m.UnderstandingEvent
                ) -> m.UnderstandingEvent:
    """``got`` (the optional understanding) with every record ``base`` (the reader's own
    ``plx.find`` of the question's identifiers) resolved that ``got`` did not: such a
    root replaces ``got``'s unresolved entity of the same words, so an understanding that
    could not resolve a record never erases one already found (UNDERSTAND-001)."""
    have = {entity.resolved.object_id for entity in got.entities
            if entity.resolved is not None}
    kept = [entity for entity in base.entities
            if entity.resolved is not None and entity.resolved.object_id not in have]
    if not kept:
        return got
    words = {normalize(entity.text) for entity in kept}
    rest = [entity for entity in got.entities
            if entity.resolved is not None or normalize(entity.text) not in words]
    got.entities = rest + kept
    return got
