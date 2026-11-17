"""The people exclusion's rule for a captured record (``integrations.people_exclusion``),
in a module of its own so a reader that applies it (``employee_permissions``,
``ontology_tools_evidence``) imports no tool layer: an integrations module may then import
the permission reader without the import graph reaching back into integrations."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# Two of people_graph's resolution states and the Entra object-id shape. They live here, and
# people_graph imports them, so this gate (reached from employee_permissions, which integrations
# import) never imports people_graph and closes no integrations -> operating import cycle.
PERSON, EXTERNAL = "person", "external"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def strings(value: Any) -> list[str]:
    """Every string inside a captured value (a sender, a membership), shape-agnostic."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in strings(v)]
    return []


def people_excluded(guard: Any, identities: Iterable[Any]) -> bool:
    """Whether a captured record naming ``identities`` is withheld from every reader now
    by the people exclusion (``integrations.people_exclusion``). A capture keeps what it
    read before a person was excluded, so the guard is re-checked whenever a mail or
    Teams capture is served. An object id is decided always; an address only by a bound
    guard (offline nothing else says whose it is). A missing or unavailable guard
    withholds every record (fail closed)."""
    from integrations.people_exclusion import PeopleExclusion, is_object_id
    if not isinstance(guard, PeopleExclusion) or guard.unavailable:
        return True
    values = {v.strip().lower() for v in strings(list(identities)) if v.strip()}
    # An address is decided by a bound guard or one the attached directory maps.
    return guard.excluded(v for v in values if guard.bound or is_object_id(v)
                          or guard.decides(v))


__all__ = ["participants_withheld", "people_excluded", "strings"]


def participants_withheld(directory: Any, participants: Iterable[Any], *,
                          sender: Any = (), sender_required: bool = False,
                          object_ids_only: bool = False) -> bool:
    """The one participant rule of every reader that can surface mail, a meeting or a
    Teams message (the rule ``operating.people_graph`` and ``operating.vendor_terms``
    apply): the record is withheld unless every person it names is cleared. ``directory``
    is an ``operating.people_graph.PeopleDirectory`` (the people exclusion over the
    captured directory); each string inside ``sender`` and ``participants`` (any shape:
    an address list, a Graph recipient, a Teams identity set) that is an Entra object id
    or a mail address is resolved by it, and the record is withheld when any resolves
    ``withheld`` (excluded) or ``undecided`` (a tenant address nothing can decide, a
    malformed address, an id two mailboxes share). An external address is a counterpart,
    never a person of ours, so it does not withhold. Other strings (display names, role
    or response words) name no identity and are not read. No directory, or one that
    cannot decide, withholds every record (fail closed); ``sender_required``: a record
    whose sender names no identity is undecided too (a message always has a sender).
    ``object_ids_only``: only object ids are identities (a Teams record: its people are
    keyed by their Entra object id, the shared key; an address beside it is the same
    member's, decided by the guard's own check, as ``people_graph`` resolves Teams members).
    Decides access only: nothing about the withheld record or its people is returned."""
    if directory is None or getattr(directory, "unavailable", "unknown"):
        return True
    senders = [s.strip().lower() for s in strings(sender) if s.strip()]
    values = [*senders, *(s.strip().lower() for s in strings(list(participants))
                          if s.strip())]
    identities = [v for v in values if _GUID.match(v) or ("@" in v and not object_ids_only)]
    if sender_required and not any("@" in v or _GUID.match(v) for v in senders):
        return True
    for value in dict.fromkeys(identities):
        found = directory.object_id(value) if _GUID.match(value) else directory.address(value)
        if getattr(found, "state", None) not in (PERSON, EXTERNAL):
            return True
    return False
