"""``plx.sql {"record": "<record handle or number>"}``: where is X, in one read (ASK-GENIE 20 #3,
19 S1): the record's card across systems.

The card is the chain read of ``ontology_tools_chain`` at one hop (hop 0 is the record
itself with the facts its type's catalog declares: status, dates, amounts; hop 1 every
record linked to it, grouped by type with its label, date, system and facts) plus the
messages that name it: every mail and Teams message whose ``records`` hold the record's
handle, newest first, as a result of its own (its own id, paged by ``{"page": id, "offset": n}``,
none cut), the newest few shown in the card. Nothing here decides what "the status" is: every
declared field is shown, and which of two records a number means is the chain read's rule (a
number that fits unlinked records comes back as candidates to pick from, never one taken).

Permissions are the door's: both reads go through ``plx.sql``'s own statement check, scope and
views, so a record, link or message the reader may not read is not in the card."""
from __future__ import annotations

from typing import Any, Mapping

from operating import ontology_tools_chain as chain
from operating.ontology_tools_identity import ToolError
from operating.pg.sql_views import _literal

SHOWN = 10   # messages shown inside the card; the result of the messages holds and pages them all


def _starts(tool: Any, result: str) -> list[str]:
    """The handles of the start records of a chain result (hop 0, direction start)."""
    from operating.ontology_tools_sql_rows import RESULTS
    kept = RESULTS.get(result.split("//", 1)[-1])
    if kept is None:
        return []
    cols = list(kept["columns"])
    hop, way, handle = cols.index("hop"), cols.index("direction"), cols.index("handle")
    return [r[handle] for r in kept["rows"]
            if r[hop] == 0 and r[way] == "start" and isinstance(r[handle], str)]


SOURCES = (
    ("emails", "SELECT 'mail' AS source, handle, received_at AS at, subject AS title, "
               "sender AS who FROM emails WHERE records && {have}"),
    ("teams_messages", "SELECT 'teams', handle, created_at, subject, author "
                       "FROM teams_messages WHERE records && {have}"))


def messages_sql(handles: list[str], views: tuple[str, ...] = ("emails", "teams_messages")
                 ) -> str:
    """One statement over the messages (mail and Teams) whose ``records`` hold any of
    ``handles``, newest first."""
    have = "ARRAY[" + ", ".join(_literal(h) for h in handles) + "]::text[]"
    parts = [sql.format(have=have) for view, sql in SOURCES if view in views]
    return " UNION ALL ".join(parts) + " ORDER BY at DESC, handle"


def card(tool: Any, conn: Any, scope: Any, actor: Any, args: Mapping[str, Any]
         ) -> dict[str, Any]:
    """The ``record`` read ``args`` asks as ``actor`` (see the module doc)."""
    asked = {"chain": args["record"], "hops": 1,
             **{k: args[k] for k in ("party", "type") if k in args}}
    out = chain.walk(tool, conn, scope, actor, asked)
    handles = [] if out.get("chain", {}).get("ambiguous") else _starts(tool, out["result"])
    mail: dict[str, Any] = {"records_named": len(handles)}
    out["note"] = (out.get("note", "") + " The card: hop 0 is the record, hop 1 its linked "
                   "records, card.newest the latest mail and Teams that name it (the rest "
                   "by page).").strip()
    if handles:
        try:
            got = tool._call({"sql": messages_sql(handles)}, actor)
        except ToolError as exc:
            # said in the card, never an empty list that reads as "no messages"
            mail["messages_unavailable"] = f"{exc.code}: {exc}"
            out["card"] = mail
            return out
        shown = got.get("rows") or []
        mail.update({"result": got["result"], "messages": got.get("profile", {}).get("rows"),
                     "columns": got.get("columns"), "newest": shown[:SHOWN],
                     "read_on": ('{"page": "%s", "offset": %d}' % (got["result"], SHOWN))
                     if len(shown) > SHOWN or got.get("continue") else None})
    else:
        mail["note"] = ("no single record was walked, so no messages were looked up: pick "
                        "one by handle, type or party")
    out["card"] = mail
    return out
