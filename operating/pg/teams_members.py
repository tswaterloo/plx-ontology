"""Who is in each Teams chat or channel (``biz.teams_messages``: ``chat_topic``,
``chat_type``, ``members``, ``addresses``, ``domains``, ``member_ids``), read from the
projected ``TeamsChat`` and ``TeamsChannel`` records in ``ontology`` (ASK-GENIE §20 #8).

Set at build (``business_comms.fill``) and at each refresh (``refresh_changed``) for both
new messages and any conversation whose membership or topic changed since the last marks.

Markings: a conversation record whose version or ``participants`` cell carries a non-empty
marking is withheld (empty member arrays, counted in ``marked_withheld``), and a marked
``topic`` / ``display_name`` cell is withheld to NULL, so marked conversation metadata
never reaches an unmarked message row. Excluded chats are already hidden per row by
``business_comms._where`` (``excluded = true`` on every message of the chat)."""
from __future__ import annotations

import json
import logging
from typing import Any

LOG = logging.getLogger("operating.pg.teams_members")

CONVERSATION_TYPES = ("TeamsChat", "TeamsChannel")
_PROPS = ("chat_id", "channel_id", "participants", "topic", "display_name",
          "chat_type", "membership_type")


def _conn(write: Any) -> Any:
    return write if hasattr(write, "cursor") else write.connection


def parse_participants(raw: Any) -> tuple[list[str], list[str], list[str], list[str]]:
    """``(members, addresses, domains, member_ids)`` from a captured ``participants`` JSON
    list (``[{id, display_name, email}]``), sorted and deduplicated."""
    if isinstance(raw, str):
        s = raw.strip()
        if not s.startswith("["):
            return [], [], [], []
        try:
            items = json.loads(s)
        except (TypeError, ValueError):
            return [], [], [], []
    elif isinstance(raw, list):
        items = raw
    else:
        return [], [], [], []
    if not isinstance(items, list):
        return [], [], [], []
    members: set[str] = set()
    addresses: set[str] = set()
    domains: set[str] = set()
    member_ids: set[str] = set()
    for p in items:
        if not isinstance(p, dict):
            continue
        email_raw = p.get("email")
        email = email_raw.strip().lower() if isinstance(email_raw, str) and "@" in email_raw else ""
        name_raw = p.get("display_name")
        name = name_raw.strip() if isinstance(name_raw, str) else ""
        uid_raw = p.get("id")
        uid = uid_raw.strip().lower() if isinstance(uid_raw, str) else ""
        if email:
            addresses.add(email)
            dom = email.rsplit("@", 1)[-1]
            if dom:
                domains.add(dom)
        if name:
            members.add(name)
        elif email:
            members.add(email)
        if uid:
            member_ids.add(uid)
    return sorted(members), sorted(addresses), sorted(domains), sorted(member_ids)


def _load_conversations(write: Any, chats: set[str] | None = None
                        ) -> tuple[dict[str, tuple[str | None, str | None, list[str], list[str],
                                                   list[str], list[str]]], int]:
    """Read current, non-removed ``TeamsChat`` / ``TeamsChannel`` metadata keyed by chat or
    channel id. Returns ``(by_chat, marked_withheld)``."""
    from ontology import store as ostore
    from operating.pg.sqlite_compat import translate
    prev_sp = write.execute("SHOW search_path").fetchone()[0]
    write.execute("SET search_path = ontology, public")
    try:
        write.execute("DROP TABLE IF EXISTS pg_temp.tm_conv_gone")
        write.execute("CREATE TEMP TABLE tm_conv_gone (object_id text PRIMARY KEY)")
        for t in CONVERSATION_TYPES:
            write.execute("INSERT INTO pg_temp.tm_conv_gone " + translate(ostore.removed_ids_sql(
                object_type_sql=":t")) + " ON CONFLICT DO NOTHING", {"t": t})
        rows = write.execute(
            "SELECT o.object_id, o.object_type, coalesce(v.markings, '[]'), f.property, c.value, "
            "coalesce(k.markings, '[]') "
            "FROM ontology.object_versions v "
            "JOIN ontology.objects o ON o.object_id = v.object_id "
            "JOIN ontology.value_sources s ON s.ver = v.ver AND s.superseded_at IS NULL "
            "JOIN ontology.value_cells c ON c.src = s.src "
            "JOIN ontology.value_fields f ON f.field = c.field "
            "LEFT JOIN ontology.value_kinds k ON k.kind = c.kind "
            "WHERE v.superseded_at IS NULL AND o.object_type = ANY(%s) "
            "AND f.property = ANY(%s) "
            "AND NOT EXISTS (SELECT 1 FROM pg_temp.tm_conv_gone g WHERE g.object_id = o.object_id)",
            (list(CONVERSATION_TYPES), list(_PROPS))).fetchall()
        write.execute("DROP TABLE IF EXISTS pg_temp.tm_conv_gone")
    finally:
        write.execute("SELECT set_config('search_path', %s, false)", (prev_sp,))

    per_obj: dict[str, dict[str, Any]] = {}
    for oid, otype, vmarks, prop, val, cmarks in rows:
        rec = per_obj.setdefault(oid, {"type": otype, "vmarks": vmarks, "vals": {}, "marks": {}})
        if val is not None and prop not in rec["vals"]:
            rec["vals"][prop] = val
        if cmarks and cmarks != "[]":
            rec["marks"][prop] = cmarks

    out: dict[str, tuple[str | None, str | None, list[str], list[str], list[str], list[str]]] = {}
    marked_withheld = 0
    for rec in per_obj.values():
        vals = rec["vals"]
        marks = rec["marks"]
        cid = vals.get("chat_id") if rec["type"] == "TeamsChat" else vals.get("channel_id")
        if not isinstance(cid, str) or not cid.strip():
            continue
        cid = cid.strip()
        if chats is not None and cid not in chats:
            continue
        if rec["vmarks"] and rec["vmarks"] != "[]":
            marked_withheld += 1
            continue
        topic_key = "topic" if rec["type"] == "TeamsChat" else "display_name"
        type_key = "chat_type" if rec["type"] == "TeamsChat" else "membership_type"
        if topic_key in marks:
            topic = None
        else:
            raw_topic = vals.get(topic_key)
            topic = raw_topic.strip() if isinstance(raw_topic, str) and raw_topic.strip() else None
        raw_type = vals.get(type_key)
        ctype = (raw_type.strip() if isinstance(raw_type, str) and raw_type.strip()
                 and type_key not in marks else None)
        if "participants" in marks:
            marked_withheld += 1
            members, addresses, domains, member_ids = [], [], [], []
        else:
            members, addresses, domains, member_ids = parse_participants(vals.get("participants"))
        out[cid] = (topic, ctype, members, addresses, domains, member_ids)
    return out, marked_withheld


def changed_chats(write: Any, lo_ver: int, hi_ver: int) -> list[str]:
    """Chat / channel ids whose ``TeamsChat`` or ``TeamsChannel`` object received a version in
    ``(lo_ver, hi_ver]`` (membership or topic change)."""
    rows = write.execute(
        "SELECT DISTINCT c.value FROM ontology.object_versions v "
        "JOIN ontology.objects o ON o.object_id = v.object_id "
        "JOIN ontology.value_sources s ON s.ver = v.ver "
        "JOIN ontology.value_cells c ON c.src = s.src "
        "JOIN ontology.value_fields f ON f.field = c.field "
        "WHERE v.ver > %s AND v.ver <= %s AND o.object_type = ANY(%s) "
        "AND f.property IN ('chat_id', 'channel_id') AND c.value IS NOT NULL",
        (int(lo_ver), int(hi_ver), list(CONVERSATION_TYPES))).fetchall()
    return sorted({r[0].strip() for r in rows if isinstance(r[0], str) and r[0].strip()})


def apply(write: Any, table: str, where: str = "true") -> dict[str, int]:
    """Populate ``chat_topic``, ``chat_type``, ``members``, ``addresses``, ``domains`` and
    ``member_ids`` on rows of ``table`` matching ``where``. Rows whose conversation has no
    current unmarked metadata are cleared to NULL / empty arrays."""
    chat_rows = write.execute(
        f"SELECT DISTINCT chat FROM {table} t WHERE chat IS NOT NULL AND ({where})").fetchall()
    chats = {r[0] for r in chat_rows if isinstance(r[0], str) and r[0]}
    got = {"rows": 0, "with_members": 0, "with_addresses": 0, "with_topic": 0,
           "no_conversation": 0, "marked_withheld": 0}
    if not chats:
        got["rows"] = write.execute(f"SELECT count(*) FROM {table} t WHERE ({where})").fetchone()[0]
        return got
    convs, marked = _load_conversations(write, chats)
    got["marked_withheld"] = marked
    write.execute("DROP TABLE IF EXISTS pg_temp.tm_members")
    write.execute(
        "CREATE TEMP TABLE tm_members ("
        "chat_key text PRIMARY KEY, chat_topic text, chat_type text, "
        "members text[], addresses text[], domains text[], member_ids text[])")
    try:
        with _conn(write).cursor().copy(
                "COPY tm_members (chat_key, chat_topic, chat_type, members, addresses, domains, "
                "member_ids) FROM STDIN") as cp:
            for cid in sorted(chats):
                if cid in convs:
                    topic, ctype, members, addrs, doms, mids = convs[cid]
                    cp.write_row((cid, topic, ctype, members, addrs, doms, mids))
                else:
                    cp.write_row((cid, None, None, [], [], [], []))
        write.execute(
            f"UPDATE {table} t SET chat_topic = m.chat_topic, chat_type = m.chat_type, "
            "members = m.members, addresses = m.addresses, domains = m.domains, "
            f"member_ids = m.member_ids FROM pg_temp.tm_members m WHERE m.chat_key = t.chat "
            f"AND ({where})")
        row = write.execute(
            f"SELECT count(*), "
            "count(*) FILTER (WHERE cardinality(members) > 0), "
            "count(*) FILTER (WHERE cardinality(addresses) > 0), "
            "count(*) FILTER (WHERE chat_topic IS NOT NULL) "
            f"FROM {table} t WHERE ({where})").fetchone()
        got["rows"] = int(row[0] or 0)
        got["with_members"] = int(row[1] or 0)
        got["with_addresses"] = int(row[2] or 0)
        got["with_topic"] = int(row[3] or 0)
        got["no_conversation"] = len(chats - set(convs))
        return got
    finally:
        write.execute("DROP TABLE IF EXISTS pg_temp.tm_members")
