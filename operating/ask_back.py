"""plx.find's ask-back for an MCP client: when a name or number matches several of the
reader's records, the answer carries ``ask_back`` (2 to ``MAX_OPTIONS`` of those records)
for the client to ask its own person which one. No word list or phrase rule decides it.
(The Workbench's own Ask has no pre-session asking back: one Ask path, owner 11-22.)"""

from __future__ import annotations

from typing import Any, Mapping, Optional

MIN_OPTIONS, MAX_OPTIONS = 2, 4

# MCP clients (slice 2):
# a person's own Claude on the remote host has no pre-session step, so plx.find's answer
# to a name that several of the reader's records answer carries ``ask_back``: 2 to
# ``MAX_OPTIONS`` of those records, for the client to ask its person which one. A server
# whose client is the Workbench's own agent (``agent_session``: the app asked back before
# the session, and no person reads the session) runs with ``ASK_BACK_SWITCH`` off.
ASK_BACK_SWITCH = "PLX_MCP_ASK_BACK"
_NOT_A_READING = frozenset({"contradicted"})
_NOT_NAMED = frozenset({"reference_match"})   # a record that holds the name, not it


def ask_back_enabled() -> bool:
    import os
    return (os.environ.get(ASK_BACK_SWITCH) or "").strip().lower() != "off"


def ask_back(arguments: Mapping[str, Any], result: Any) -> Optional[dict[str, Any]]:
    """``{"why": "several_records_match", "options": [object_ref]}`` when ``result`` is
    plx.find's ``ambiguous`` answer to a name or number (a ``query``, optionally typed or
    limited; never a filter, a later page, a home, portfolio, entities or text answer) with at least two distinct
    records matched by a key, an identifier or a name (a typed binding of any tier but
    contradicted; never a record that only refers to the name, a repeated row or words
    found in some text). The options are the result's own
    records (already read as the reader), best rank first; anything else is None."""
    from operating.injection_guard import QUERY_TEXT_KIND
    if not isinstance(arguments, Mapping) or not isinstance(result, Mapping):
        return None
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip() or any(
            arguments.get(k) not in (None, "", [], {}) for k in (
                "filters", "mode", "cursor", "text")):
        return None
    if result.get("status") != "ambiguous" or any(
            result.get(k) for k in ("home", "portfolio", "structured", "entities")):
        return None
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in result.get("candidates") or ():
        if not isinstance(c, Mapping) or c.get("repeated") or c.get("tier") in _NOT_A_READING \
                or c.get("reason") in _NOT_NAMED:
            continue
        matched, ref = c.get("matched"), c.get("object")
        if not isinstance(matched, Mapping) or not isinstance(ref, Mapping) \
                or matched.get("identifier_kind") == QUERY_TEXT_KIND:
            continue
        oid = ref.get("object_id")
        if not isinstance(oid, str) or oid in seen:
            continue
        seen.add(oid)
        options.append(dict(ref))
        if len(options) >= MAX_OPTIONS:
            break
    if len(options) < MIN_OPTIONS:
        return None
    return {"why": "several_records_match", "options": options}


__all__ = ["ASK_BACK_SWITCH", "ask_back", "ask_back_enabled"]
