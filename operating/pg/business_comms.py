"""Communications as business tables (schema ``biz``, ARCH-FINAL-1117 section 1): one row per
captured message, chat message or calendar event, with its date, thread, people, mailbox
and the business rows it is about, so "emails with Take 5 last month" is a join on
``emails`` and ``customers``, never a crawl of the store. Bodies stay in the document
text index (``ontology.text_fields``): ``comms_search`` finds and opens them through the
same guarded views (one ranked statement; one item's text by its handle).

Built set-based inside Postgres (no record is loaded into Python; 3.3M served emails):
``fill`` writes ``<view>_next`` in bounded statements, one per range of version numbers
(``CHUNK`` versions), from temporary tables read once per build (the type's removed
records, its links, the biz rows' object ids); ``business_views.build_view`` swaps the
table in as for every view. ``refresh_changed`` updates the live table in place, by the
versions and links recorded since the last build or refresh (``state.notes`` keeps the
high-water marks) and the records removed since, in one transaction.

What a row holds to (the business views' rules where they apply):
- One row per record (a mailbox copy of a message is its own record: each copy has its own
  mailbox and so its own permission; ``message_id`` names the message across copies).
- Only records their source still holds (``ontology.store.removed_ids_sql``).
- Permissions: ``audience_keys`` = the record's audience key (``mail:<mailbox>``,
  ``teams:chat:<id>``, ``meetings:calendar:<owner>`` ...: the staff scope's units,
  ``operating.staff_scope``); ``rec_marks`` = its version's markings and every non-empty
  marking of a cell read (a marked cell hides the row, never just a value); a link is
  followed only when unmarked. The people exclusion and Freightcom are decided in the
  guarded view (``sql_views``, ``SCOPE_COMMS``), per statement, never baked in here.
- ``ver``: the version a row was read from, so the guarded view can tell a record the
  people exclusion has decided (``covered_ver``) from one recorded since (withheld until
  ``people_excluded update`` decides it: fail closed)."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from operating.pg import business_views as bv

CHUNK = 200_000   # versions per insert statement (bounded statement and memory)
WRITE_CHUNK = 250_000   # stage rows per statement of the build's write of its table
# The business rows a message may be about, by the view whose handle it names.
PARTY_VIEWS = {"customer": "customers", "supplier": "suppliers"}
RECORD_VIEWS = ("sales_orders", "sales_invoices", "purchase_orders", "products",
                "supplier_invoices")
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,80}$")


@dataclass(frozen=True)
class Spec:
    """One comms table: the record types it lists, the properties it reads (as columns),
    and the link types whose targets it names."""
    view: str
    types: tuple[str, ...]
    props: tuple[str, ...]
    when: str            # the property that dates a row
    select: str          # SQL: the row's columns from ``p`` (the pivoted properties)
    party_links: tuple[str, ...]   # suffixes: <Type>.<suffix> -> Customer / Supplier
    attachments: bool = False
    mail: bool = False   # the mail columns (mailbox kind, bulk sender, addresses, domains)
    keep: str = "true"   # SQL over ``p``: the records listed (e.g. not deleted at the source)
    # who may read a row (``sql_views``): "audience" = the record's audience key held;
    # "teams" = also the reader's visible chat history (``teams_windows``) covers it
    scope: str = "audience"
    # True: the text is not in the record's cells but in the version's document text
    # (``ontology.text_fields``): read there for Freightcom; a row without it is withheld
    text_in_fields: bool = False
    # SQL over the table's own columns: the header text its ``words`` hold (search)
    header: str = "subject"
    # True: the row carries ``participants`` (its captured item ACL's identities, read from
    # the meetings capture by handle at build and refresh): scope "participants" admits a
    # reader holding the unit only when one of their identities is among them, or a full
    # reader (``transcript_reader``)
    participants: bool = False


def _addresses(*exprs: str) -> str:
    """SQL: the lower-cased addresses of these JSON-list or plain-address expressions."""
    parts = []
    for e in exprs:
        parts.append(f"CASE WHEN left({e}, 1) = '[' THEN ARRAY(SELECT lower(btrim(x)) FROM "
                     f"json_array_elements_text({e}::json) x) WHEN strpos({e}, '@') > 0 THEN "
                     f"ARRAY[lower(btrim({e}))] ELSE '{{}}'::text[] END")
    return "(" + " || ".join(parts) + ")"


def _ts(e: str) -> str:
    return (f"CASE WHEN {e} ~ '^[12][0-9]{{3}}-[01][0-9]-[0-3][0-9]' "
            f"THEN ({e})::timestamptz END")


def _day(e: str) -> str:
    return f"(({_ts(e)}) AT TIME ZONE '{bv.BUSINESS_TZ}')::date"


def _ts_floor(e: str) -> str:
    """``_ts`` with the fraction cut to the microsecond (never rounded up): the Teams
    capture compares a message's creation, so cut, with a member's history start."""
    return _ts(f"regexp_replace({e}, '([.][0-9]{{6}})[0-9]+', '\\1')")


EMAIL = Spec(
    "emails", ("Email",),
    ("subject", "sender", "to_addresses", "cc_addresses", "received_at", "sent_at",
     "conversation_id", "internet_message_id", "mailbox", "body_preview"),
    "received_at",
    f"{_ts('p.received_at')}, {_day('p.received_at')}, {_ts('p.sent_at')}, p.subject, "
    "lower(btrim(p.sender)), split_part(lower(btrim(p.sender)), '@', 2), "
    f"{_addresses('p.to_addresses')}, {_addresses('p.cc_addresses')}, p.conversation_id, "
    "p.internet_message_id, lower(btrim(p.mailbox))",
    ("sent_by_customer", "mentions_customer", "sent_by_supplier", "mentions_supplier"),
    attachments=True, mail=True, text_in_fields=True,
    header="subject, sender, array_to_string(to_addresses, ' '), "
           "array_to_string(cc_addresses, ' ')")
EMAIL_COLS = (
    ("received_at", "timestamp", "when the mailbox received it"),
    ("received_date", "date", "the day it was received (business time zone)"),
    ("sent_at", "timestamp", "when it was sent"),
    ("subject", "text", "the subject line (untrusted text)"),
    ("sender", "text", "the From address, lower-case"),
    ("sender_domain", "text", "the From address's domain"),
    ("to_addresses", "list", "the To addresses, lower-case"),
    ("cc_addresses", "list", "the Cc addresses, lower-case"),
    ("thread", "text", "the conversation (thread) id: messages of one thread share it"),
    ("message_id", "text", "the Internet Message-ID: copies of one message in several "
                           "mailboxes share it (count distinct message_id for messages)"),
    ("mailbox", "text", "the mailbox's owner id (one row per mailbox copy)"),
)


# Door v2 (ARCH-FINAL-1117 section 14): the read rule as small columns decided at build and
# refresh. ``unit``: the row's (audience key, record marks) as an id of ``biz.comms_units``
# (ids are never reused or renumbered); ``ai_ok``: false when the row names Freightcom, NULL
# when that cannot be decided yet (fail closed: a message whose body text is not indexed
# yet is withheld from the AI until a refresh or the nightly ``recheck`` reads it);
# ``excluded``: the record is in the people exclusion's set
# (``ontology.people_excluded_objects``) as it stood at the stamp kept in ``state``;
# ``words``: the header's words (``ontology.sq_words``, the word index's tokenizer) for
# ``comms_search`` (the body's are in ``ontology.chunk_words``).
INTERNAL = (("ver", "bigint"), ("unit", "integer"), ("ai_ok", "boolean"),
            ("excluded", "boolean"), ("words", "tsvector"))


# the lineage and permission columns a comms row carries after its own (the business views'
# door v2 moved theirs to side tables; a comms row is read by ``unit``, these are the
# inputs of ``unit`` and the citation, never shown)
COMMON = (("handle", "text"), ("object_ids", "text[]"), ("systems", "text[]"),
          ("copies", "integer"), ("audience_keys", "text[]"), ("rec_marks", "text[]"),
          ("col_marks", "jsonb"), ("cites", "jsonb"))


def columns(v: Any) -> list[tuple[str, str]]:
    return ([(c.name, bv.SQL_TYPES[c.kind]) for c in v.cols] + list(COMMON)
            + list(v.internal))


def build_view(v: Any, write: Any, *, as_of: dt.date, digest: str | None = None
               ) -> dict[str, Any]:
    """Build ``<schema>.<v.name>`` set-based (``fill``) and swap it in with its guarded view,
    in one transaction each (``business_views.build_view`` hands a comms table here)."""
    import time
    name = v.name
    guarded = bv._guarded_views()
    leaning = bv.dependent_views(write, name)
    if leaning and guarded is None:
        raise RuntimeError(f"{bv.SCHEMA}.{name} has views over it ({', '.join(leaning)}): "
                           "the swap re-creates them only through operating.pg.sql_views")
    started = time.monotonic()
    nxt, old = f"{name}_next", f"{name}_old"
    cols = ", ".join(f'"{n}" {t}' for n, t in columns(v))
    try:
        with write.transaction():
            write.execute(f"DROP TABLE IF EXISTS {bv.SCHEMA}.{nxt}")
            write.execute(f"CREATE TABLE {bv.SCHEMA}.{nxt} ({cols})")
            notes = {"as_of": as_of.isoformat(), "marks": dict(v.fill(write, nxt, as_of))}
            count = write.execute(f"SELECT count(*) FROM {bv.SCHEMA}.{nxt}").fetchone()[0]
    except BaseException:
        # the stage the readers committed outlives the rolled-back transaction
        drop_stage(write, nxt)
        raise
    seconds = round(time.monotonic() - started, 2)
    with write.transaction():
        if guarded is not None:
            guarded.drop(write, name, bv.SCHEMA)
        write.execute(f"DROP TABLE IF EXISTS {bv.SCHEMA}.{old}")
        write.execute(f"ALTER TABLE IF EXISTS {bv.SCHEMA}.{name} RENAME TO {old}")
        write.execute(f"ALTER TABLE {bv.SCHEMA}.{nxt} RENAME TO {name}")
        write.execute(f"DROP TABLE IF EXISTS {bv.SCHEMA}.{old}")
        if guarded is not None:
            guarded.create(write, v, bv.SCHEMA)
        write.execute(
            f"INSERT INTO {bv.SCHEMA}.state (view, built_at, digest, rows, seconds, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (view) DO UPDATE SET "
            "built_at = excluded.built_at, digest = excluded.digest, rows = excluded.rows, "
            "seconds = excluded.seconds, notes = excluded.notes",
            (name, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), digest,
             count, seconds, json.dumps(notes)))
        write.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                      f"'plx_api') THEN EXECUTE 'GRANT SELECT ON {bv.SCHEMA}.{name} TO "
                      "plx_api'; END IF; END $$")
    return {"view": name, "rows": count, "seconds": seconds}


def _register(spec: Spec, doc: str, cols: Sequence[tuple[str, str, str]]) -> None:
    common = (
        ("mailbox_shared", "boolean", "true: a shared mailbox (sales@, orders@ ...), "
                                      "false: a person's own; null: not known"),
        ("bulk_sender", "boolean", "true: an automated or bulk sender (no-reply, "
                                   "notifications, newsletters): never a person waiting"),
        ("is_bulk", "boolean", "true: mail a mailing platform or an automated system sent "
                               "(its sender, its form, or the sending platform its stored "
                               "envelope names): owed no reply; our own people's mail only "
                               "by its sender"),
        ("owner_role", "text", "the mailbox owner's place on this copy, by every address "
                               "they have: 'to' (in To), 'cc' (only in Cc), 'none'; null: "
                               "the mailbox's addresses are not known"),
        ("owner_replied_at", "timestamp", "the latest message the mailbox owner sent in "
                                          "this thread (same mailbox) after this one; null: "
                                          "none since, or the owner's addresses not known"),
        ("attachments", "integer", "how many attachments it carries"),
        ("addresses", "list", "every address on it (from, to, cc), lower-case"),
        ("domains", "list", "the domains of those addresses"),
    ) if spec.mail else ()
    linked = (
        ("customers", "list", "handles of the customers rows it is from or names "
                              "(join customers.handle = ANY(customers))"),
        ("suppliers", "list", "handles of the suppliers rows it is from or names"),
        ("records", "list", "handles of the order, invoice, PO and product rows it names"),
    )
    made = [bv.Col(n, k, d) for n, k, d in (*cols, *common, *linked)]
    internal = INTERNAL + ((("participants", "text[]"),) if spec.participants else ())
    bv.VIEWS[spec.view] = bv.View(spec.view, doc, made, lambda rd, as_of: iter(()),
                                  spec.types, fill=lambda w, t, a: fill(spec, w, t),
                                  internal=internal)


_register(EMAIL, "Every captured email, one row per mailbox copy (count distinct message_id "
          "for messages): when, who (from/to/cc addresses and domains), the mailbox and "
          "whether it is shared, the thread, attachments, whether the sender is automated "
          "or bulk, and the customers, suppliers and business records it is from or names "
          "(their handles). The body is not here: open the handle to read it.", EMAIL_COLS)


TEAMS = Spec(
    "teams_messages", ("TeamsChatMessage",),
    ("created_at", "from", "chat_id", "subject", "message_type", "reply_to_id", "deleted_at"),
    "created_at",
    f"{_ts_floor('p.created_at')}, {_day('p.created_at')}, p.\"from\", p.chat_id, "
    "p.subject, p.message_type, p.reply_to_id",
    (), keep="coalesce(btrim(p.deleted_at), '') = ''", scope="teams", text_in_fields=True,
    header="subject, sender")
TEAMS_COLS = (
    ("created_at", "timestamp", "when it was posted"),
    ("created_date", "date", "the day it was posted (business time zone)"),
    ("sender", "text", "who posted it, as captured"),
    ("chat", "text", "the chat's id: messages of one chat share it"),
    ("subject", "text", "the subject, when one was set (untrusted text)"),
    ("message_type", "text", "the kind of message (message, systemEventMessage ...)"),
    ("reply_to", "text", "the id of the message it replies to, if any"),
)
_register(TEAMS, "Every captured Teams chat message you may read (deleted ones left out): "
          "when, who posted it, the chat, and the customers, suppliers and business records "
          "it names (their handles). The text is not here: search finds it, open the handle "
          "to read it.", TEAMS_COLS)


def _json_field(e: str, key: str) -> str:
    """SQL: ``key`` of the JSON object text ``e`` (lower-cased, trimmed), else NULL."""
    return (f"CASE WHEN left(btrim({e}), 1) = '{{' THEN lower(btrim(({e})::json->>'{key}')) "
            "END")


def _json_list(e: str, key: str | None) -> str:
    """SQL: the elements (``key`` of each object, lower-cased; None: the plain strings) of
    the JSON list text ``e``, else empty."""
    item = f"lower(btrim(x->>'{key}'))" if key else "btrim(x #>> '{}')"
    return (f"CASE WHEN left(btrim({e}), 1) = '[' THEN ARRAY(SELECT {item} FROM "
            f"json_array_elements(({e})::json) x WHERE {item} <> '') ELSE '{{}}'::text[] END")


# Meetings:
# one row per calendar's copy of an event, read by whoever may read that
# calendar (``meetings:calendar:<owner>``: a staff reader their own; the people exclusion
# and Freightcom as for mail: an event naming Freightcom in any cell is kept from the AI);
# transcripts in their organizer's unit (``meetings:organizer:<owner>``), Freightcom decided
# on the spoken text (``text_in_fields``). The capture never stores an event or transcript
# naming an excluded person; the ``excluded`` flag holds the set as it grows. A transcript
# is the item rule's (``CaseRuntime`` item gate, ``meeting_participants``): its unit AND one
# of the reader's identities among its captured participants (organizer and the cleared
# attendees, ``participants``), or a full reader's grant; never the unit alone.
_START, _END = 'p."start"', 'p."end"'   # quoted: ``end`` is an SQL keyword
MEETING = Spec(
    "meetings", ("CalendarEvent",),
    ("subject", "organizer", "attendees", "start", "end", "location", "is_cancelled",
     "is_online_meeting", "is_organizer", "is_all_day", "ical_uid", "mailbox"),
    "start_at",
    _ts(_START) + ", " + _day(_START) + ", " + _ts(_END) + ", p.subject, "
    f"{_json_field('p.organizer', 'address')}, {_json_list('p.attendees', 'address')}, "
    "p.location, p.is_cancelled = 'true', p.is_online_meeting = 'true', "
    "p.is_organizer = 'true', p.is_all_day = 'true', p.ical_uid, lower(btrim(p.mailbox))",
    (), header="subject, organizer, array_to_string(attendees, ' '), location")
MEETING_COLS = (
    ("start_at", "timestamp", "when it starts"),
    ("start_date", "date", "the day it starts (business time zone)"),
    ("end_at", "timestamp", "when it ends"),
    ("subject", "text", "the subject (untrusted text; empty for a private event)"),
    ("organizer", "text", "the organizer's address, lower-case"),
    ("attendees", "list", "the attendees' addresses, lower-case"),
    ("location", "text", "where (room or place), as the invitation states it"),
    ("is_cancelled", "boolean", "the event was cancelled"),
    ("is_online", "boolean", "a Teams (online) meeting"),
    ("is_organizer", "boolean", "the calendar's owner organizes it"),
    ("is_all_day", "boolean", "an all-day event"),
    ("ical_uid", "text", "the meeting's id: every attendee's copy shares it (count "
                         "distinct ical_uid for meetings); meeting_transcripts joins on it"),
    ("mailbox", "text", "the calendar owner's id (one row per calendar copy)"),
)
_register(MEETING, "Calendar events (meetings) on the calendars you may read, one row per "
          "calendar copy (count distinct ical_uid for meetings): when, subject, organizer, "
          "attendees, place, online or not, cancelled or not, and the customers, suppliers "
          "and records it names. Join meeting_transcripts on ical_uid for what was said.",
          MEETING_COLS)


TRANSCRIPT = Spec(
    "meeting_transcripts", ("MeetingTranscript",),
    ("meeting_start", "meeting_end", "subject", "organizer", "ical_uid", "speakers",
     "cue_count", "created_at"),
    "meeting_start",
    f"{_ts('p.meeting_start')}, {_day('p.meeting_start')}, {_ts('p.meeting_end')}, "
    f"p.subject, lower(btrim(p.organizer)), p.ical_uid, {_json_list('p.speakers', None)}, "
    "CASE WHEN p.cue_count ~ '^[0-9]{1,9}$' THEN p.cue_count::integer END, "
    f"{_ts('p.created_at')}",
    (), text_in_fields=True, header="subject, array_to_string(speakers, ' ')",
    scope="participants", participants=True)
TRANSCRIPT_COLS = (
    ("meeting_start", "timestamp", "when the meeting started"),
    ("meeting_date", "date", "the day of the meeting (business time zone)"),
    ("meeting_end", "timestamp", "when the meeting ended"),
    ("subject", "text", "the meeting's subject (untrusted text)"),
    ("organizer", "text", "the organizer's id"),
    ("ical_uid", "text", "the meeting's id (join meetings on ical_uid)"),
    ("speakers", "list", "who spoke (display names)"),
    ("cue_count", "integer", "how many spoken lines it holds"),
    ("created_at", "timestamp", "when the transcript was made"),
)
_register(TRANSCRIPT, "Teams meeting transcripts you may read (meetings you took part in; "
          "owner: all): when, subject, who spoke, and the customers, suppliers and records "
          "it names. The text is not here: search finds it, open the handle to read it.",
          TRANSCRIPT_COLS)


# --------------------------------------------------------------------------- build


def _prep(write: Any, spec: Spec, ids: str | None = None) -> None:
    """The temporary tables a fill reads: removed records of the types, the biz rows'
    object ids and (for the full build) the link and attachment counts of the types."""
    from ontology import store as ostore
    from operating.pg.sqlite_compat import translate
    write.execute("SET LOCAL search_path = ontology, public")
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_gone, pg_temp.cm_biz")
    write.execute("CREATE TEMP TABLE cm_gone (object_id text PRIMARY KEY)")
    for t in spec.types:
        write.execute("INSERT INTO cm_gone " + translate(ostore.removed_ids_sql(
            object_type_sql=":t")) + " ON CONFLICT DO NOTHING", {"t": t})
    write.execute("CREATE TEMP TABLE cm_biz (oid text, handle text, kind text)")
    for kind, view in [*PARTY_VIEWS.items(), *((v, v) for v in RECORD_VIEWS)]:
        if write.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.{view}",)
                         ).fetchone()[0] is None:
            continue
        # door v2: a business row's object ids are in the lineage side table (by view)
        if _has_col(write, view, "object_ids"):
            write.execute(f"INSERT INTO cm_biz SELECT DISTINCT unnest(object_ids), handle, "
                          f"%s FROM {bv.SCHEMA}.{view}", (kind,))
        else:
            write.execute(f"INSERT INTO cm_biz SELECT DISTINCT unnest(l.object_ids), "
                          f"l.handle, %s FROM {bv.SCHEMA}.lineage l WHERE l.view = %s",
                          (kind, view))
    write.execute("CREATE INDEX ON cm_biz (oid)")
    # attachments per message, read once (a probe per row would read the links by their
    # target); ``ids``: only these messages (a refresh)
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_attach")
    write.execute("CREATE TEMP TABLE cm_attach AS SELECT to_object AS oid, count(*) AS n "
                  "FROM links WHERE link_type = 'Attachment.attached_to' AND superseded_at "
                  "IS NULL" + (f" AND to_object IN (SELECT object_id FROM {ids})" if ids
                               else "") + (" AND false" if not spec.attachments else "")
                  + " GROUP BY to_object")
    write.execute("CREATE INDEX ON cm_attach (oid)")
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_text")
    if spec.mail:
        write.execute("CREATE TEMP TABLE cm_text (n bigserial PRIMARY KEY, object_id text, "
                      "sender text, subject text, preview text)")
    write.execute("ANALYZE cm_gone")
    write.execute("ANALYZE cm_biz")


def _has_col(write: Any, table: str, col: str) -> bool:
    return write.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                         "table_schema = %s AND table_name = %s AND column_name = %s)",
                         (bv.SCHEMA, table, col)).fetchone()[0]


def _links_sql(spec: Spec, ids: str) -> str:
    """SQL: per record of ``ids`` (a relation of object ids), its party and record handles
    (unmarked live links whose target is a biz row)."""
    types = [f"{t}.{s}" for t in spec.types for s in spec.party_links]
    likes = " OR ".join([f"l.link_type IN ({', '.join(bv_lit(x) for x in types)})"]
                        * bool(types) + [f"starts_with(l.link_type, {bv_lit(t + '.mentions_')})"
                           for t in spec.types])
    return (
        "SELECT l.from_object AS oid, "
        "array_agg(DISTINCT b.handle) FILTER (WHERE b.kind = 'customer') AS customers, "
        "array_agg(DISTINCT b.handle) FILTER (WHERE b.kind = 'supplier') AS suppliers, "
        "array_agg(DISTINCT b.handle) FILTER (WHERE b.kind NOT IN ('customer', "
        "'supplier')) AS records "
        f"FROM links l JOIN cm_biz b ON b.oid = l.to_object "
        f"WHERE l.from_object IN (SELECT object_id FROM {ids}) AND l.superseded_at IS NULL "
        f"AND l.markings = '[]' AND l.from_object <> l.to_object AND ({likes}) "
        "GROUP BY l.from_object")


def bv_lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _rows_sql(spec: Spec, table: str, where: str, units: bool, text: str = "cm_text") -> str:
    """SQL: insert the rows of the records whose current version matches ``where`` (mail:
    and their text the bulk rules read into ``text``)."""
    pivot = ", ".join(f"min(c.value) FILTER (WHERE f.property = {bv_lit(p)}) AS \"{p}\""
                      for p in spec.props)
    props = ", ".join(bv_lit(p) for p in spec.props)
    shared = ("(SELECT u.sharing = 'shared' FROM search.audience_units u WHERE "
              "u.audience_key = r.audience_key LIMIT 1)" if units else "NULL::boolean")
    attach = "coalesce(at.n, 0)::integer" if spec.attachments else "0"
    mail = (f"{shared}, NULL::boolean, NULL::boolean, NULL::text, NULL::timestamptz, "
            f"{attach}, NULL::text[], NULL::text[], " if spec.mail else "")
    # mail: the text the bulk rules read (``_flags``), kept for this build or refresh only
    text = (f", tx AS (INSERT INTO {text} (object_id, sender, subject, preview) SELECT "
            "p.object_id, lower(btrim(p.sender)), p.subject, p.body_preview FROM p)"
            if spec.mail else "")
    from operating.pg.sql_views import _literal
    from integrations.freightcom_rule import sql_match
    fc_cell = sql_match("c.value", _literal)
    # Freightcom: a cell of the version names it; the document text of ``text_in_fields``
    # types (the mail body, the Teams message) is read after the insert (``_text_ai``)
    ai_ok = "NOT p.fc"
    return (
        f"WITH r AS MATERIALIZED (SELECT o.object_id, o.audience_key, v.ver, v.l1_handle, "
        "v.markings FROM object_versions v JOIN objects o ON o.object_id = v.object_id "
        "WHERE v.superseded_at IS NULL AND o.object_type IN ("
        + ", ".join(bv_lit(t) for t in spec.types) + f") AND {where} "
        "AND NOT EXISTS (SELECT 1 FROM cm_gone g WHERE g.object_id = o.object_id)), "
        f"p AS (SELECT r.object_id, {pivot}, array_agg(DISTINCT k.markings) FILTER "
        f"(WHERE f.property IN ({props}) AND k.markings IS NOT NULL AND k.markings <> '[]') "
        f"AS cell_marks, coalesce(bool_or({fc_cell}), false) AS fc "
        "FROM r JOIN value_sources s ON s.ver = r.ver AND s.superseded_at IS NULL "
        "CROSS JOIN LATERAL (SELECT c.value, c.field, c.kind FROM value_cells c "
        "WHERE c.src = s.src OFFSET 0) c JOIN value_fields f ON f.field = c.field "
        "LEFT JOIN value_kinds k ON k.kind = c.kind GROUP BY r.object_id), "
        f"lk AS ({_links_sql(spec, 'r')}){text} "
        f"INSERT INTO {bv.SCHEMA}.{table} SELECT {spec.select}, {mail}"
        "coalesce(lk.customers, '{}'), "
        "coalesce(lk.suppliers, '{}'), coalesce(lk.records, '{}'), "
        "r.l1_handle, ARRAY[r.object_id], ARRAY[split_part(r.l1_handle, '://', 1)], 1, "
        "ARRAY[coalesce(r.audience_key, '')], "
        "ARRAY(SELECT DISTINCT m FROM unnest(ARRAY['[]', coalesce(r.markings, '[]')] || "
        "coalesce(p.cell_marks, '{}')) m ORDER BY m), '{}'::jsonb, '{}'::jsonb, r.ver, "
        f"NULL::integer, {ai_ok}, NULL::boolean, NULL::tsvector FROM r JOIN p ON p.object_id = r.object_id LEFT JOIN lk ON lk.oid = "
        f"r.object_id LEFT JOIN cm_attach at ON at.oid = r.object_id WHERE {spec.keep}")


def _units(write: Any, table: str = "search.audience_units") -> bool:
    return write.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None


# The derived columns, ONE definition each: the build writes them in its one INSERT ... SELECT
# of the final table (``_compose``), a refresh sets them on its own rows (``_finish``,
# ``_flags``, ``_classify``). Unqualified column names: the row's own (a statement over one
# table, or the build's row subquery).
ADDRESSES = ("ARRAY(SELECT DISTINCT a FROM unnest(ARRAY[sender] || to_addresses || "
             "cc_addresses) a WHERE strpos(a, '@') > 0 ORDER BY a)")
DOMAINS = ("ARRAY(SELECT DISTINCT split_part(a, '@', 2) FROM unnest(ARRAY[sender] || "
           "to_addresses || cc_addresses) a WHERE strpos(a, '@') > 0 ORDER BY 1)")


def _words(spec: Spec) -> str:
    return f"ontology.sq_words(concat_ws(' ', {spec.header}))"


# over the row ``{s}`` and its verdicts: ``sv`` (cm_senders), ``f`` (cm_flag), ``u``
# (comms_units), ``a`` (cm_ai)
BULK_SENDER = "sv.bulk"
FLAG_COLS = (("is_bulk", "CASE WHEN f.mine THEN f.is_bulk ELSE {s}.is_bulk END"),
             ("owner_role", "CASE WHEN f.mine THEN f.owner_role ELSE {s}.owner_role END"),
             ("owner_replied_at", "CASE WHEN f.last_sent > f.received_at THEN f.last_sent "
                                  "END"))
UNIT = "u.unit"
UNIT_JOIN = ("u.audience_key = {s}.audience_keys[1] AND u.marks = {s}.rec_marks AND "
             "cardinality({s}.audience_keys) = 1")
AI_OK = "CASE WHEN {s}.ai_ok IS NOT FALSE AND a.handle IS NOT NULL THEN a.ok ELSE {s}.ai_ok END"


def _senders(write: Any, table: str, where: str = "true") -> None:
    """``cm_senders``: the bulk-sender verdict of each distinct sender of ``table``'s rows
    matching ``where`` whose flag is not set, by the inbox's own rules (Python, one string
    each)."""
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_senders")
    write.execute("CREATE TEMP TABLE cm_senders (sender text PRIMARY KEY, bulk boolean)")
    senders = [r[0] for r in write.execute(
        f"SELECT DISTINCT sender FROM {table} WHERE bulk_sender IS NULL "
        f"AND sender IS NOT NULL AND {where}")]
    with write.cursor().copy("COPY cm_senders (sender, bulk) FROM STDIN") as cp:
        for s in senders:
            cp.write_row((s, bulk_sender(s)))
    write.execute("ANALYZE cm_senders")


def _finish(write: Any, table: str, where: str = "true") -> None:
    """A refresh's rows (``where``, just written): addresses, domains and the bulk-sender
    flag, by the build's own definitions (two UPDATEs of those rows only)."""
    t = f"{bv.SCHEMA}.{table}"
    _senders(write, t, where)
    write.execute(f"UPDATE {t} SET addresses = {ADDRESSES}, domains = {DOMAINS} WHERE {where}")
    write.execute(f"UPDATE {t} SET bulk_sender = {BULK_SENDER} FROM cm_senders sv WHERE "
                  f"sv.sender = {t}.sender AND {t}.bulk_sender IS NULL AND ({where})")


# --------------------------------------------------------------------------- mail flags
#
# ``is_bulk``, ``owner_role`` and ``owner_replied_at`` (alerts: "needs a reply") decided once
# at build and refresh, by main's reviewed rules: ``inbox_triage.bulk`` (the sender, the form
# of the subject and own text), ``mail_envelope`` (the sending platform the stored envelope
# names, read-only from the mail capture) with ``inbox_triage.by_platform``'s exemptions (a
# platform's relay of a person's message; a thread one of ours wrote in before it), and the
# mailbox owner's every address (the mail capture's ``mailboxes``: UPN and proxy addresses).

FLAG_BATCH = 2_000     # rows per bulk-rule batch (one envelope read per batch)
FLAG_POOL_MIN = 20_000  # fewer rows than this: read in this process
FLAG_WORKERS = 8


def capture_db() -> Any:
    """The mail capture the flags read (``PLX_STATE_DIR``, the host's state directory)."""
    import os
    from pathlib import Path
    return Path(os.environ.get("PLX_STATE_DIR") or "/data/plx-state") / "mail-capture" \
        / "capture.db"


def mailbox_addresses(db: Any) -> dict[str, list[str]] | None:
    """Per mailbox id (lower-case) every address of its owner (UPN and proxy addresses,
    lower-case), read-only from the mail capture ``db``; None when it cannot be read (the
    owner's place and replies are then unknown: null, never guessed)."""
    import sqlite3
    from pathlib import Path
    if not Path(db).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return None
    out: dict[str, set[str]] = {}
    try:
        for box, upn, identities in conn.execute(
                "SELECT mailbox_id, upn, identities FROM mailboxes"):
            try:
                listed = json.loads(identities or "[]")
            except (TypeError, ValueError):
                listed = []
            for a in [upn, *(listed if isinstance(listed, list) else [])]:
                if not isinstance(a, str):
                    continue
                a = a.strip().lower()
                a = a[5:] if a.startswith("smtp:") else a
                if "@" in a and ":" not in a and box:
                    out.setdefault(str(box).strip().lower(), set()).add(a)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {k: sorted(v) for k, v in out.items()}


def _bulk_batch(db: str, rows: Sequence[tuple[str, str, str, str]]
                ) -> list[tuple[str, bool, bool | None, bool]]:
    """Per row ``(object id, sender, subject, preview)`` whose form or envelope says bulk:
    ``(object id, form, platform, relay)``. The text read is triage's (``_messages``): the
    subject unless a reply or forward, then the own text of the preview (quoted history
    and gateway notices cut). ``platform``: the stored envelope names a mailing platform
    (None: no envelope held: no signal)."""
    from operating import inbox_triage as it, proactive_threads as pt
    from operating.mail_envelope import RELAY, reader
    sent = reader(db)([r[0] for r in rows])
    out = []
    for oid, sender, subject, preview in rows:
        subject = subject or ""
        own = pt._own(preview or "")
        head = "" if it._REPLY.match(subject) or it._FORWARD.match(subject) else subject
        text = (head + ".\n" + own) if head else own
        form = it.bulk(sender or "", text, subject)
        platform = sent.get(oid)
        if form or platform:
            out.append((oid, form, platform, bool(RELAY.search(text))))
    return out


COLLECT_BYTES = 64 * 1024 ** 2   # read since the last collect: then collect
_read_since = [0]


def _collect(rows: Sequence[Sequence[Any]]) -> None:
    """Free the query results read so far once ``COLLECT_BYTES`` of them were read. A
    psycopg result sits in a reference cycle that only the cyclic collector frees, and a
    batch of text makes few container objects, so the collector seldom runs: read batch
    after batch, the process kept ~2-3x every byte it had read (measured 11-18 on served
    mail: 100k bodies, 532 MB, held 1.6 GB; collecting as it reads held it flat). 11-18 the
    emails build was killed past its 8 GB this way, twice."""
    import gc
    _read_since[0] += sum(len(v) for r in rows for v in r if isinstance(v, (bytes, str)))
    if _read_since[0] >= COLLECT_BYTES:
        _read_since[0] = 0
        gc.collect()


def _bulk_rows(write: Any, db: Any, text: str = "cm_text") -> None:
    """``cm_bulk``: the bulk-rule verdicts of ``text``'s rows (this fill's or refresh's),
    a batch at a time, over a process pool when there are many (the rules are Python)."""
    import os
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_bulk")
    write.execute("CREATE TEMP TABLE cm_bulk (object_id text PRIMARY KEY, form boolean, "
                  "platform boolean, relay boolean)")
    total = write.execute(f"SELECT count(*), coalesce(max(n), 0) FROM {text}").fetchone()
    batches = ((lo, lo + FLAG_BATCH) for lo in range(0, int(total[1]), FLAG_BATCH))

    def read(lo: int, hi: int) -> list[tuple[str, str, str, str]]:
        got = write.execute(f"SELECT object_id, sender, subject, preview FROM {text} "
                            "WHERE n > %s AND n <= %s", (lo, hi)).fetchall()
        _collect(got)
        return got

    # each batch's verdicts are written as they come (memory: the batches in flight, never
    # the table's; 11-18 a build was killed past its 8 GB holding whole-table lists)
    def put(found: Sequence[tuple[str, bool, bool | None, bool]]) -> None:
        if found:
            with write.cursor().copy("COPY cm_bulk (object_id, form, platform, relay) FROM "
                                     "STDIN") as cp:
                for row in found:
                    cp.write_row(row)
    if int(total[0]) < FLAG_POOL_MIN:
        for lo, hi in batches:
            put(_bulk_batch(str(db), read(lo, hi)))
    else:
        import concurrent.futures as cf
        from ontology import parent_watch
        workers = max(1, min(FLAG_WORKERS, os.cpu_count() or 1))
        # the platform's default start (fork): a worker's parent is this process, so the
        # parent watch holds (a forkserver's workers are the server's children)
        with cf.ProcessPoolExecutor(workers, **parent_watch.pool_init()) as pool:
            running: set[Any] = set()
            for lo, hi in batches:
                running.add(pool.submit(_bulk_batch, str(db), read(lo, hi)))
                if len(running) >= 2 * workers:   # bounded rows in flight
                    done, running = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                    for f in done:
                        put(f.result())
            for f in cf.as_completed(running):
                put(f.result())
    write.execute("ANALYZE cm_bulk")


def _flags(write: Any, table: str, threads: str | None = None, *,
           stage: bool = False, text: str = "cm_text") -> dict[str, Any]:
    """``is_bulk`` and ``owner_role`` of this fill's or refresh's rows (``cm_text``) and
    ``owner_replied_at`` of every row of the threads they are in (``threads``: a relation of
    ``(mailbox, thread)`` also to read, e.g. of rows a refresh dropped; None: the whole
    table). ``stage`` (a build): ``table`` (schema-qualified) is the build's stage, its rows
    unflagged, and the verdicts are left in ``cm_flag`` by the stage's row number (``sn``)
    for the build's one write of the final table (``_compose``); else (a refresh) one
    UPDATE of those rows of ``biz.<table>``. ``text``: the table the rows' text is in (the
    build's readers commit theirs beside the stage). Returns what the flags were read from."""
    t = table if stage else f"{bv.SCHEMA}.{table}"
    db = capture_db()
    boxes = mailbox_addresses(db)
    domains = sorted({a.rsplit("@", 1)[1] for v in (boxes or {}).values() for a in v})
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_owner")
    write.execute("CREATE TEMP TABLE cm_owner (mailbox text PRIMARY KEY, addrs text[])")
    if boxes:
        with write.cursor().copy("COPY cm_owner (mailbox, addrs) FROM STDIN") as cp:
            for box, addrs in boxes.items():
                cp.write_row((box, addrs))
    _bulk_rows(write, db, text)
    # Every rule below is a join on deduplicated, indexed, ANALYZEd temp tables or a
    # per-row expression: never a per-row subplan. ``x IN (SELECT .. FROM cm_text)`` in the
    # select list could not become a join: over the served 3.3M rows the set no longer fit
    # work_mem, so Postgres re-read the whole spilled set for every row (n x m: the second
    # served build sat 90 min in one statement, 11-18).
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_mine")
    write.execute(f"CREATE TEMP TABLE cm_mine AS SELECT DISTINCT object_id FROM {text}")
    write.execute("CREATE UNIQUE INDEX ON cm_mine (object_id)")
    write.execute("ANALYZE cm_mine")
    pattern = _internal_pattern(domains)

    def internal(alias: str) -> str:   # ``proactive_threads._internal``: a tenant domain
        # = d, or ends with '.' || d: one regular expression of every tenant domain (the
        # ``right(..) = '.' || d`` probe of each, without a subplan per row); its NULLs are
        # the old rule's (an unknown sender domain: unknown when any domain is known)
        return (f"({alias}.sender_domain = ANY(%(d)s::text[]) OR "
                + (f"{alias}.sender_domain ~ %(rx)s)" if pattern else "false)"))
    params = {"d": domains, "rx": pattern}
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_keys")
    # the bulk-sender flag: the stage's is not set yet (its verdict is cm_senders')
    sent_bulk = "coalesce(sv.bulk, false)" if stage else "coalesce(t.bulk_sender, false)"
    if stage:
        rows = f"{t} t LEFT JOIN cm_senders sv ON sv.sender = t.sender"
    elif threads is None:   # every row of the table
        rows = f"{t} t"
    else:
        # a refresh: every row of the threads to read again (``threads``) or of this
        # refresh's rows, and this refresh's rows themselves (one with no thread too)
        write.execute("DROP TABLE IF EXISTS pg_temp.cm_scope")
        write.execute(
            f"CREATE TEMP TABLE cm_scope AS SELECT mailbox, thread FROM {threads} "
            f"WHERE thread IS NOT NULL UNION SELECT t3.mailbox, t3.thread FROM {t} t3 "
            "JOIN cm_mine m ON m.object_id = t3.object_ids[1] WHERE t3.thread IS NOT NULL")
        write.execute("ANALYZE cm_scope")
        write.execute(
            f"CREATE TEMP TABLE cm_keys AS SELECT t.object_ids[1] AS oid FROM {t} t "
            "JOIN cm_scope s ON s.thread = t.thread AND s.mailbox = t.mailbox "
            "UNION SELECT object_id FROM cm_mine")
        write.execute("CREATE UNIQUE INDEX ON cm_keys (oid)")
        write.execute("ANALYZE cm_keys")
        rows = f"{t} t JOIN cm_keys k ON k.oid = t.object_ids[1]"
    # mail a platform sent stays a person's in a thread one of ours wrote in before it:
    # the first message one of ours wrote per thread (any mailbox), read ONCE. A per-row
    # EXISTS over the thread was a correlated subplan probing every message of the thread
    # for each platform row (a thread of n platform messages cost n^2 reads; the first
    # served build read 2.7 TB in 40 min there, 11-18).
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_first")
    if threads is None:
        first_of = f"{t} o2 WHERE o2.thread IS NOT NULL"
    else:
        write.execute("DROP TABLE IF EXISTS pg_temp.cm_tids")
        write.execute(f"CREATE TEMP TABLE cm_tids AS SELECT DISTINCT t.thread FROM {rows} "
                      "WHERE t.thread IS NOT NULL")
        write.execute("ANALYZE cm_tids")
        first_of = f"{t} o2 JOIN cm_tids ti ON ti.thread = o2.thread WHERE true"
    write.execute(
        f"CREATE TEMP TABLE cm_first AS SELECT o2.thread, min(o2.received_at) AS at "
        f"FROM {first_of} AND {internal('o2')} GROUP BY o2.thread", params)
    write.execute("CREATE UNIQUE INDEX ON cm_first (thread)")
    write.execute("ANALYZE cm_first")
    earlier = "coalesce(fi.at < t.received_at, false)"
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_flag")
    write.execute(
        "CREATE TEMP TABLE cm_flag AS SELECT t.object_ids[1] AS oid, "
        + ("t.sn, " if stage else "") +
        f"m.object_id IS NOT NULL AS mine, {sent_bulk} OR "
        f"(NOT {internal('t')} AND "
        "coalesce(b.form OR (b.platform AND NOT b.relay AND NOT "
        f"{earlier}), false)) AS is_bulk, "
        "CASE WHEN o.mailbox IS NULL THEN NULL WHEN o.addrs && t.to_addresses THEN 'to' "
        "WHEN o.addrs && t.cc_addresses THEN 'cc' ELSE 'none' END AS owner_role, "
        "CASE WHEN o.mailbox IS NOT NULL AND t.thread IS NOT NULL THEN "
        "max(coalesce(t.sent_at, t.received_at)) "
        "FILTER (WHERE t.sender = ANY(o.addrs)) OVER (PARTITION BY t.mailbox, t.thread) "
        "END AS last_sent, t.received_at "
        f"FROM {rows} LEFT JOIN cm_mine m ON m.object_id = t.object_ids[1] "
        "LEFT JOIN cm_owner o ON o.mailbox = t.mailbox LEFT JOIN cm_bulk b ON "
        "b.object_id = t.object_ids[1] LEFT JOIN cm_first fi ON fi.thread = t.thread",
        params)
    write.execute(f"CREATE INDEX ON cm_flag ({'sn' if stage else 'oid'})")
    write.execute("ANALYZE cm_flag")
    if not stage:
        write.execute(
            f"UPDATE {t} t SET "
            + ", ".join(f"{c} = {e.format(s='t')}" for c, e in FLAG_COLS)
            + " FROM cm_flag f WHERE f.oid = t.object_ids[1]")
    return {"capture": boxes is not None, "mailboxes": len(boxes or {})}


def _internal_pattern(domains: Sequence[str]) -> str | None:
    """A regular expression (Postgres ARE) matching a sender domain that ends with '.' and
    one of ``domains`` (a subdomain of a tenant domain); None when there is none."""
    if not domains:
        return None
    return "\\.(" + "|".join(re.sub(r"([^A-Za-z0-9_])", r"\\\1", d) for d in domains) + ")$"


def bulk_sender(address: str) -> bool:
    """An automated or bulk sender by its address alone (``my_people.automated``,
    ``inbox_triage.BULK_LOCAL`` / ``BULK_DOMAIN``)."""
    from operating import inbox_triage, my_people
    a = (address or "").strip().lower()
    return bool(a) and (my_people.automated(a) or bool(inbox_triage.BULK_LOCAL.match(a))
                        or bool(inbox_triage.BULK_DOMAIN.search(a)))


def marks(write: Any) -> dict[str, int]:
    """The store's high-water marks a refresh continues from (``field``: the document text
    fields, so a body indexed after its message is read at the next refresh)."""
    ver, link, field = write.execute(
        "SELECT (SELECT max(ver) FROM ontology.object_versions), "
        "(SELECT max(row_no) FROM ontology.link_rows), "
        "(SELECT max(field_id) FROM ontology.text_fields)").fetchone()
    return {"ver": int(ver or 0), "link": int(link or 0), "field": int(field or 0)}


# The build stops (its transaction rolled back: ``<view>_next`` and the stage dropped,
# the served table kept) when the database's disk would fall below this many GB free
# (11-18: a build that rewrote its table ten times took /data down 1.7 GB a minute). 25:
# above the host's own disk guard (20 GB), checked per chunk and before the table write
# with its size; 45 was set while that build rewrote its table, and with /data at 44.6 GB
# free it refused even the meetings tables (a few thousand rows) on every refresh, 11-18.
MIN_FREE_GB_DEFAULT = 25.0


def _min_free_bytes() -> int:
    import os
    raw = os.environ.get("PLX_BIZ_MIN_FREE_GB", "").strip()
    try:
        gb = float(raw) if raw else MIN_FREE_GB_DEFAULT
    except ValueError:
        raise RuntimeError(f"PLX_BIZ_MIN_FREE_GB must be a number of GB, not {raw!r}")
    if not gb >= 0:
        raise RuntimeError(f"PLX_BIZ_MIN_FREE_GB must be a number of GB, not {raw!r}")
    return int(gb * 1024 ** 3)


def disk_path(write: Any) -> str:
    """The directory whose filesystem the build writes to: ``PLX_BIZ_DISK_PATH``, else the
    database's data directory when this role may read it, else the host's state directory
    (``PLX_STATE_DIR``, ``/data/plx-state``: the served database's disk)."""
    import os
    path = os.environ.get("PLX_BIZ_DISK_PATH", "").strip()
    if path:
        return path
    try:
        row = write.execute("SELECT setting FROM pg_settings WHERE name = "
                            "'data_directory'").fetchone()
    except Exception:   # noqa: BLE001 - a role without the setting: the host's disk
        row = None
    if row and row[0] and os.path.isdir(row[0]):
        return row[0]
    return os.environ.get("PLX_STATE_DIR") or "/data/plx-state"


def free_bytes(path: str) -> int:
    import os
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _disk_guard(write: Any, path: str, step: str, need: int = 0) -> None:
    """Stop the build (raise: its transaction rolls back) when the disk holds less than the
    floor free, or would after ``need`` more bytes. A disk that cannot be read stops it too
    (fail closed)."""
    try:
        free = free_bytes(path)
    except OSError as exc:
        raise RuntimeError(f"build stopped before {step}: the free space of {path} cannot "
                           f"be read ({exc})") from exc
    floor = _min_free_bytes()
    if free - need < floor:
        gb = 1024 ** 3
        raise RuntimeError(
            f"disk below the {floor / gb:.0f} GB floor (PLX_BIZ_MIN_FREE_GB), build stopped "
            f"before {step}; the new table is dropped, the served one kept: "
            f"{free / gb:.1f} GB free on {path}"
            + (f", {need / gb:.1f} GB more needed" if need else ""))


def read_workers() -> int:
    """How many connections read a build's rows at once: ``PLX_BIZ_READ_WORKERS``, else
    every core (A build uses the whole box). Not a positive whole number:
    refused, never read as one."""
    import os
    raw = os.environ.get("PLX_BIZ_READ_WORKERS", "").strip()
    if not raw:
        return max(1, os.cpu_count() or 1)
    try:
        n = int(raw)
    except ValueError:
        n = 0
    if n < 1:
        raise RuntimeError(f"PLX_BIZ_READ_WORKERS must be a whole number of 1 or more, "
                           f"not {raw!r}")
    return n


def stage_names(table: str) -> tuple[str, str]:
    """The build's stage tables beside ``biz.<table>``: its rows, and (mail) their text."""
    return f"{table}_stage", f"{table}_stage_text"


def drop_stage(write: Any, table: str) -> None:
    """Drop a build's stage tables (committed by its readers, so a rolled-back build leaves
    them: its ``build_view`` drops them after; a killed one, ``sweep_leftovers``)."""
    try:
        write.execute("DROP TABLE IF EXISTS " + ", ".join(
            f"{bv.SCHEMA}.{n}" for n in stage_names(table)))
    except Exception:  # noqa: BLE001 - the build's own failure is what is reported
        pass


def _connector(write: Any) -> Any:
    """New connections to ``write``'s database as ``write``'s role (its parameters and
    password), autocommit."""
    import psycopg
    info = write.info
    extra = {"password": info.password} if getattr(info, "password", None) else {}
    return lambda: psycopg.connect(info.dsn, autocommit=True, **extra)


def _read_rows(spec: Spec, write: Any, table: str, top: int, disk: str, units: bool,
               connect: Any = None) -> None:
    """Fill the stage (made empty and committed) with the rows of every version up to
    ``top``: ``CHUNK``-version ranges taken from one queue by ``read_workers()``
    connections at once, each with its own temporary tables (``_prep``) and transaction.
    The rows read are what one connection reading every range reads (each range is read
    once, by one reader; the stage numbers the rows, ``sn``). Any reader failing stops the
    others (their statements cancelled) and raises here."""
    import queue
    import threading
    import concurrent.futures as cf
    stage, text = stage_names(table)
    ranges: queue.SimpleQueue[tuple[int, int]] = queue.SimpleQueue()
    count = 0
    for lo in range(0, int(top), CHUNK):
        ranges.put((lo, min(lo + CHUNK, int(top))))
        count += 1
    if not count:
        return
    connect = connect or _connector(write)
    timeout = write.execute("SHOW statement_timeout").fetchone()[0]
    stop = threading.Event()
    live: list[Any] = []
    lock = threading.Lock()

    def reader() -> None:
        conn = connect()
        with lock:
            live.append(conn)
        try:
            if stop.is_set():
                return
            conn.execute("SELECT set_config('statement_timeout', %s, false)", (timeout,))
            with conn.transaction():
                _prep(conn, spec)
                while not stop.is_set():
                    try:
                        lo, hi = ranges.get_nowait()
                    except queue.Empty:
                        return
                    _disk_guard(conn, disk, f"versions {lo}..{hi}")
                    conn.execute(_rows_sql(spec, stage, f"v.ver > {int(lo)} AND v.ver <= "
                                           f"{int(hi)}", units, f"{bv.SCHEMA}.{text}"))
                if stop.is_set():   # another reader failed: roll this one back too
                    raise RuntimeError("stopped: another reader failed")
        finally:
            conn.close()

    workers = max(1, min(read_workers(), count))
    pool = cf.ThreadPoolExecutor(workers, thread_name_prefix=f"read-{table}")
    try:
        running = [pool.submit(reader) for _ in range(workers)]
        for f in cf.as_completed(running):
            f.result()   # the first failure raises (and stops the rest below)
    except BaseException:
        stop.set()
        with lock:
            for conn in live:
                try:
                    conn.cancel()
                except Exception:  # noqa: BLE001 - closed already
                    pass
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def fill(spec: Spec, write: Any, table: str, *, connect: Any = None) -> dict[str, Any]:
    """Fill ``biz.<table>`` (made, empty) with every row of ``spec``, writing each row ONCE:
    the rows go to an unlogged stage, read by every core at once (``_read_rows``: one
    statement per ``CHUNK`` versions, each range by one of ``read_workers()`` connections;
    the stage and the mail text are committed by them, so ``build_view`` drops them if this
    build rolls back); every derived column (addresses, flags, unit, words, the Freightcom
    and people flags) is decided in small verdict tables keyed by the stage row or its
    sender, handle or audience; then one ``INSERT ... SELECT`` joins them into the table
    (``_compose``) and the stage is dropped. No UPDATE of the table: each one left a dead
    copy of it (11-18). Peak extra disk: the stage (less than the final table). Returns the
    high-water marks it read up to (rows of later versions: the next refresh's).
    ``connect``: opens a reader's connection (default: ``write``'s database and role)."""
    got = marks(write)
    disk = disk_path(write)
    _disk_guard(write, disk, "reading the rows")
    t = f"{bv.SCHEMA}.{table}"
    stage, text = (f"{bv.SCHEMA}.{n}" for n in stage_names(table))
    cols = ", ".join(f'"{n}" {ty}' for n, ty in columns(bv.VIEWS[spec.view]))
    side = (connect or _connector(write))()
    try:   # committed (the readers' own connections write it), outside this transaction
        side.execute(f"DROP TABLE IF EXISTS {stage}, {text}")
        side.execute(f"CREATE UNLOGGED TABLE {stage} ({cols}, sn bigserial)")
        if spec.mail:
            side.execute(f"CREATE UNLOGGED TABLE {text} (n bigserial PRIMARY KEY, "
                         "object_id text, sender text, subject text, preview text)")
    finally:
        side.close()
    _read_rows(spec, write, table, int(got["ver"]), disk, _units(write), connect)
    write.execute("SET LOCAL search_path = ontology, public")
    # the Freightcom text check reads, the flags and the write join the stage by these
    write.execute(f"CREATE INDEX ON {stage} (handle)")
    write.execute(f"CREATE INDEX ON {stage} (sn)")
    write.execute(f"ANALYZE {stage}")
    if spec.mail:
        _disk_guard(write, disk, "the mail flags")
        _senders(write, stage)
        got["flags"] = _flags(write, stage, stage=True, text=text)
    _ensure_units(write)
    write.execute(f"INSERT INTO {bv.SCHEMA}.comms_units (audience_key, marks) SELECT DISTINCT "
                  f"audience_keys[1], rec_marks FROM {stage} WHERE cardinality(audience_keys) "
                  "= 1 AND audience_keys[1] <> '' ON CONFLICT DO NOTHING")
    if spec.text_in_fields:
        _disk_guard(write, disk, "the Freightcom text check")
        _ai_verdicts(write, stage, "true")
    got["people"] = people_stamp(write)
    if got["people"] is not None:   # the people set, deduplicated once (1.4M served)
        write.execute("DROP TABLE IF EXISTS pg_temp.cm_people")
        write.execute("CREATE TEMP TABLE cm_people AS SELECT DISTINCT object_id FROM "
                      "ontology.people_excluded_objects")
        write.execute("CREATE UNIQUE INDEX ON cm_people (object_id)")
        write.execute("ANALYZE cm_people")
    size, top = write.execute(f"SELECT pg_total_relation_size(%s::regclass), "
                              f"coalesce(max(sn), 0) FROM {stage}", (stage,)).fetchone()
    _disk_guard(write, disk, "writing the table", need=int(size))
    # the stage's rows in ranges of its row number: each row written once, each statement
    # bounded (the build's statement cap), the disk checked between them
    for lo in range(0, int(top), WRITE_CHUNK):
        _disk_guard(write, disk, f"writing the table (row {lo})")
        write.execute(_compose(spec, stage, t, people=got["people"] is not None,
                               rows=(lo, lo + WRITE_CHUNK)))
    write.execute(f"DROP TABLE {stage}" + (f", {text}" if spec.mail else ""))
    _disk_guard(write, disk, "the indexes")
    write.execute(f"CREATE INDEX ON {t} ((object_ids[1]))")
    write.execute(f"CREATE INDEX ON {t} (handle)")
    if spec.participants:   # the meetings' participant sets (a refresh: ``_classify``)
        _participants(write, t, "true")
    if spec.mail:
        write.execute(f"CREATE INDEX ON {t} (thread, mailbox)")
    write.execute(f"CREATE INDEX ON {t} ({spec.when} DESC)")
    write.execute(f"CREATE INDEX ON {t} (unit, {spec.when} DESC)")
    for col in ("addresses", "domains", "customers", "suppliers", "records"):
        if any(c.name == col for c in bv.VIEWS[spec.view].cols):
            write.execute(f"CREATE INDEX ON {t} USING gin ({col})")
    write.execute(f"CREATE INDEX ON {t} USING gin (words)")
    write.execute(f"ANALYZE {t}")
    return got


def _compose(spec: Spec, stage: str, table: str, *, people: bool,
             rows: tuple[int, int]) -> str:
    """SQL: the write of the final table: the stage rows numbered in ``rows`` (lo, hi]
    with their derived columns (the definitions a refresh sets too) joined from the verdict
    tables. ``people``: the people exclusion has a set (``cm_people``; else ``excluded``
    stays NULL: the guarded view shows no row)."""
    derived = {"unit": UNIT, "words": "s.d_words"}
    joins = [f"LEFT JOIN {bv.SCHEMA}.comms_units u ON {UNIT_JOIN.format(s='s')}"]
    inner = [f"{_words(spec)} AS d_words"]
    if spec.mail:
        inner += [f"{ADDRESSES} AS d_addresses", f"{DOMAINS} AS d_domains"]
        derived.update(addresses="s.d_addresses", domains="s.d_domains",
                       bulk_sender=f"CASE WHEN s.bulk_sender IS NULL THEN {BULK_SENDER} "
                                   "ELSE s.bulk_sender END")
        derived.update((c, e.format(s="s")) for c, e in FLAG_COLS)
        joins += ["LEFT JOIN cm_senders sv ON sv.sender = s.sender",
                  "LEFT JOIN cm_flag f ON f.sn = s.sn"]
    if spec.text_in_fields:
        derived["ai_ok"] = AI_OK.format(s="s")
        joins.append("LEFT JOIN cm_ai a ON a.handle = s.handle")
    if people:   # in the set as it stands (deduplicated: one row per row)
        derived["excluded"] = "pe.object_id IS NOT NULL"
        joins.append("LEFT JOIN cm_people pe ON pe.object_id = s.object_ids[1]")
    names = [n for n, _ in columns(bv.VIEWS[spec.view])]
    select = ", ".join(derived.get(n, f's."{n}"') for n in names)
    cols = ", ".join(f'"{n}"' for n in names)
    return (f"INSERT INTO {table} ({cols}) SELECT {select} FROM (SELECT s.*, "
            + ", ".join(inner) + f" FROM {stage} s WHERE s.sn > {int(rows[0])} AND s.sn <= "
            f"{int(rows[1])}) s " + " ".join(joins))


SPECS: dict[str, Spec] = {s.view: s for s in (EMAIL, TEAMS, MEETING, TRANSCRIPT)}


def refresh_changed(write: Any, view: str, since: Mapping[str, Any], *,
                    digest: str | None = None) -> dict[str, Any]:
    """Update the live ``biz.<view>`` in place (one transaction): the rows of records with a
    version or an outgoing link recorded after ``since`` (``{"ver", "link"}``, the marks
    of the last build or refresh) are read again, and rows of records removed or superseded
    since are dropped. Returns the new marks and the counts. Bounded: only the changed
    records are read (the removed set is read by the type index)."""
    spec = SPECS[view]
    lo_ver, lo_link = int(since.get("ver") or 0), int(since.get("link") or 0)
    # marks of a build before the text mark: every text field is new to it (fail closed)
    lo_field = int(since.get("field") or 0)
    with write.transaction():
        got = marks(write)
        write.execute("SET LOCAL search_path = ontology, public")
        types = ", ".join(bv_lit(t) for t in spec.types)
        write.execute("DROP TABLE IF EXISTS pg_temp.cm_changed")
        write.execute(
            "CREATE TEMP TABLE cm_changed AS SELECT DISTINCT v.object_id FROM "
            "object_versions v JOIN objects o ON o.object_id = v.object_id "
            f"WHERE v.ver > %s AND v.ver <= %s AND o.object_type IN ({types}) UNION "
            "SELECT l.from_object FROM link_rows l JOIN objects o ON o.object_id = "
            f"l.from_object WHERE l.row_no > %s AND l.row_no <= %s AND o.object_type IN "
            f"({types}) UNION SELECT l.to_object FROM link_rows l WHERE l.row_no > %s AND "
            "l.row_no <= %s AND l.link_type = 'Attachment.attached_to' UNION "
            # a body indexed since (the field ids' primary key: no scan of the text)
            "SELECT f.object_id FROM text_fields f JOIN objects o ON o.object_id = "
            f"f.object_id WHERE f.field_id > %s AND f.field_id <= %s AND o.object_type IN "
            f"({types})",
            (lo_ver, got["ver"], lo_link, got["link"], lo_link, got["link"], lo_field,
             got["field"]))
        write.execute("ANALYZE cm_changed")
        _prep(write, spec, "cm_changed")
        # the rows to drop: one semi-join of the changed and the removed ids (two ``IN``s
        # joined by OR are per-row subplans: a scan of the whole table each refresh, or
        # n x m once a set outgrows work_mem), by the table's object id index
        drop = ("t.object_ids[1] IN (SELECT object_id FROM cm_changed UNION "
                "SELECT object_id FROM cm_gone)")
        if spec.mail:   # the threads of rows dropped here: their replies are read again
            write.execute("DROP TABLE IF EXISTS pg_temp.cm_threads")
            write.execute(f"CREATE TEMP TABLE cm_threads AS SELECT DISTINCT mailbox, thread "
                          f"FROM {bv.SCHEMA}.{view} t WHERE {drop}")
        dropped = write.execute(f"DELETE FROM {bv.SCHEMA}.{view} t WHERE {drop}").rowcount
        write.execute(_rows_sql(spec, view, "o.object_id IN (SELECT object_id FROM "
                                             "cm_changed)", _units(write)))
        added = write.execute(f"SELECT count(*) FROM {bv.SCHEMA}.{view} WHERE object_ids[1] "
                              "IN (SELECT object_id FROM cm_changed)").fetchone()[0]
        if spec.mail:
            _finish(write, view, "object_ids[1] IN (SELECT object_id FROM cm_changed)")
            got["flags"] = _flags(write, view, "cm_threads")
        got["people"] = _classify(write, spec, view,
                                  "object_ids[1] IN (SELECT object_id FROM cm_changed)",
                                  since.get("people"))
        rows = write.execute(f"SELECT count(*) FROM {bv.SCHEMA}.{view}").fetchone()[0]
        write.execute(
            f"UPDATE {bv.SCHEMA}.state SET built_at = %s, digest = %s, rows = %s, "
            "notes = %s WHERE view = %s",
            (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), digest, rows,
             json.dumps({"as_of": bv.today().isoformat(), "marks": got}), view))
    return {"view": view, "dropped": dropped, "read": added, "rows": rows, "marks": got}


def _ensure_units(write: Any) -> None:
    write.execute(f"CREATE TABLE IF NOT EXISTS {bv.SCHEMA}.comms_units (unit serial PRIMARY "
                  "KEY, audience_key text NOT NULL, marks text[] NOT NULL, "
                  "UNIQUE (audience_key, marks))")


def people_stamp(write: Any) -> list[Any] | None:
    """The people exclusion's set as it stands (its list tag, build, update and covered
    version), or None when there is none: the stamp the ``excluded`` flags are of."""
    if write.execute("SELECT to_regclass('ontology.people_excluded_state')").fetchone()[0] \
            is None:
        return None
    row = write.execute("SELECT exclusion, built_at, coalesce(updated_at, ''), covered_ver "
                        "FROM ontology.people_excluded_state").fetchone()
    return None if row is None else [row[0], row[1], row[2], int(row[3])]


def _classify(write: Any, spec: Spec, table: str, where: str,
              before: Sequence[Any] | None = None) -> list[Any] | None:
    """A refresh's door columns of the rows matching ``where`` (just written; a build writes
    them with its rows, ``_compose``, by the same definitions): their unit, the
    Freightcom check of their document text (``text_in_fields``) and the people flag. When
    the exclusion's set changed since ``before`` (the stamp the table's flags are of) every
    row's flag is read again. Returns the stamp the flags are now of (None: no set, the
    guarded view then shows no row)."""
    t = f"{bv.SCHEMA}.{table}"
    _ensure_units(write)
    write.execute(f"INSERT INTO {bv.SCHEMA}.comms_units (audience_key, marks) SELECT DISTINCT "
                  f"audience_keys[1], rec_marks FROM {t} WHERE cardinality(audience_keys) = 1 "
                  f"AND audience_keys[1] <> '' AND ({where}) ON CONFLICT DO NOTHING")
    write.execute(f"UPDATE {t} SET unit = {UNIT} FROM {bv.SCHEMA}.comms_units u WHERE "
                  f"{UNIT_JOIN.format(s=t)} AND ({where})")
    write.execute(f"UPDATE {t} SET words = {_words(spec)} WHERE ({where})")
    if spec.text_in_fields:
        _text_ai(write, t, where)
    if spec.participants:
        _participants(write, t, where)
    stamp = people_stamp(write)
    if stamp is None:
        return None
    peo = "SELECT 1 FROM ontology.people_excluded_objects p WHERE p.object_id = {}.object_ids[1]"
    # set-based (a semi-join, then the rest), never an EXISTS probe per row: the rows of
    # ``where`` are this fill's or refresh's (written with no flag), each written once
    write.execute(f"UPDATE {t} SET excluded = NULL WHERE excluded IS NOT NULL AND ({where})")
    write.execute(f"UPDATE {t} SET excluded = true WHERE {t}.object_ids[1] IN (SELECT "
                  f"p.object_id FROM ontology.people_excluded_objects p) AND ({where})")
    write.execute(f"UPDATE {t} SET excluded = false WHERE excluded IS NULL AND ({where})")
    if before is not None and list(before) != stamp:
        write.execute(f"UPDATE {t} SET excluded = true FROM ontology.people_excluded_objects "
                      f"p WHERE p.object_id = {t}.object_ids[1] AND {t}.excluded IS NOT TRUE")
        write.execute(f"UPDATE {t} SET excluded = false WHERE excluded AND NOT EXISTS "
                      f"({peo.format(t)})")
    return stamp


TEXT_BATCH = 5_000
ACL_KIND = {"meeting-transcript": "meeting_participants"}


def meetings_db() -> Any:
    """The meetings capture (``PLX_STATE_DIR``): a transcript's participants are read here."""
    import os
    from pathlib import Path
    from integrations.capture_meetings import DB_FILE, STORE_DIR
    return Path(os.environ.get("PLX_STATE_DIR") or "/data/plx-state") / STORE_DIR / DB_FILE


def captured_participants(db: Any, handles: Sequence[str]) -> dict[str, list[str]]:
    """Per handle the identities (lower-case) of the captured item ACL its version holds:
    only a ``derived`` ACL of the scheme's kind (``ACL_KIND``) naming a list; a handle of
    another capture generation, not in the store, or of any other ACL is absent (its row
    then admits full readers only). Read-only; an unreadable store: nothing."""
    import sqlite3
    from integrations.capture_store import parse_handle
    out: dict[str, list[str]] = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        try:
            got = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        except sqlite3.Error:
            return out
        generation = got[0] if got else None
        for handle in handles:
            scheme = handle.partition("://")[0]
            try:
                kind, _path, gen, capture_id = parse_handle(handle)
            except (ValueError, LookupError):
                continue
            if scheme not in ACL_KIND or generation is None or gen != generation:
                continue
            found = conn.execute(
                "SELECT payload FROM versions WHERE capture_id = ? AND kind = ? AND path = ?",
                (capture_id, kind.name, handle.partition("://")[2].rpartition("@")[0])
            ).fetchone()
            try:
                acl = (json.loads(found[0]).get("meta") or {}).get("source_acl") \
                    if found else None
            except (TypeError, ValueError, AttributeError):
                continue
            ids = acl.get("identities") if isinstance(acl, Mapping) else None
            if acl is None or acl.get("status") != "derived" \
                    or acl.get("kind") != ACL_KIND[scheme] or not isinstance(ids, list):
                continue
            out[handle] = sorted({i.strip().lower() for i in ids
                                  if isinstance(i, str) and i.strip()})
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


def _participants(write: Any, table: str, where: str) -> None:
    """``participants`` of the rows matching ``where`` from the meetings capture (one read
    of the rows' handles, written in batches); a row whose ACL cannot be read keeps none."""
    handles = [r[0] for r in write.execute(
        f"SELECT handle FROM {table} WHERE handle IS NOT NULL AND ({where})").fetchall()]
    write.execute(f"UPDATE {table} SET participants = '{{}}' WHERE ({where})")
    if not handles:
        return
    found = captured_participants(meetings_db(), handles)
    pairs = sorted(found.items())
    for lo in range(0, len(pairs), TEXT_BATCH):
        part = pairs[lo:lo + TEXT_BATCH]
        write.execute(
            f"UPDATE {table} t SET participants = ARRAY(SELECT json_array_elements_text("
            "x.ids::json)) FROM unnest(%s::text[], %s::text[]) x(h, ids) WHERE t.handle = x.h",
            ([h for h, _ in part], [json.dumps(i) for _, i in part]))


def _text_ai(write: Any, table: str, where: str) -> None:
    """``ai_ok`` of the rows matching ``where`` (a refresh's, the nightly recheck's): the
    verdicts of ``_ai_verdicts`` set on those rows only (the build joins them instead)."""
    _ai_verdicts(write, table, where)
    write.execute(f"UPDATE {table} t SET ai_ok = {AI_OK.format(s='t')} FROM cm_ai a WHERE "
                  f"a.handle = t.handle AND t.ai_ok IS NOT FALSE AND ({where})")


# the document text of (handle, object id) pairs, as the guarded open reads it: by the version
# handle and the record (only committed ``ontology`` tables: a pool worker's own connection)
TEXT_SQL = ("SELECT f.version_handle, f.field_id, c.start, c.body FROM unnest(%s::text[], "
            "%s::text[]) AS t(handle, oid) JOIN ontology.text_fields f ON f.version_handle = "
            "t.handle AND f.object_id = t.oid JOIN ontology.text_chunks c ON c.field_id = "
            "f.field_id ORDER BY 1, 2, 3")
_TEXT_CONN: list[Any] = []   # a pool worker's own connection (``_text_worker``)


def _text_verdicts(conn: Any, pairs: Sequence[tuple[str, str]]) -> dict[str, bool]:
    """Read the text of ``pairs`` and decide it (``_ai_batch``); the results freed as read."""
    rows = [(h, f, st, bytes(body)) for h, f, st, body in conn.execute(
        TEXT_SQL, ([h for h, _ in pairs], [o for _, o in pairs]))]
    _collect(rows)
    return _ai_batch(rows)


def _text_worker(dsn: str, password: str | None, timeout: str) -> None:
    """A pool worker's start: its own read-only connection to the build's database."""
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True, **({"password": password} if password
                                                   else {}))
    conn.execute("SELECT set_config('statement_timeout', %s, false), set_config("
                 "'default_transaction_read_only', 'on', false)", (timeout,))
    _TEXT_CONN[:] = [conn]


def _text_part(pairs: Sequence[tuple[str, str]]) -> dict[str, bool]:
    return _text_verdicts(_TEXT_CONN[0], pairs)


def _ai_verdicts(write: Any, table: str, where: str) -> None:
    """``cm_ai`` (by handle): Freightcom in the document text of the rows matching ``where``
    whose cells do not name it (``ai_ok`` not false; the mail body and the Teams message are
    in no cell): its fields read back from their stored pieces (zlib, so in Python: this
    database has no inflate) and checked by the shared rule, a page of rows at a time. A row
    with no document text yet is undecided (``ai_ok`` NULL: withheld from the AI until its
    text is indexed and read: the refresh after, or ``recheck``).

    Memory is bounded, never the table's: this process pages the (handle, object id) pairs
    (``TEXT_BATCH``, keyset by the handle index) and writes each page's verdicts as they
    come; when there are many, the text is read, decompressed and checked in the pool's
    workers, each over its own read-only connection (every core reads; this process never
    holds a body). 11-18: the 3.3M served handles' text read through this process held
    2-3x every byte (psycopg results freed only by the cyclic collector) and the emails
    build was killed past its 8 GB, twice."""
    import itertools
    import os
    write.execute("DROP TABLE IF EXISTS pg_temp.cm_ai")
    write.execute("CREATE TEMP TABLE cm_ai (handle text PRIMARY KEY, ok boolean)")

    def pages() -> Any:   # (handle, object id) pairs in handle order (keyset: no offset)
        after: str | None = None
        while True:
            got = write.execute(
                f"SELECT handle, array_agg(DISTINCT object_ids[1]) FROM {table} WHERE ai_ok "
                f"IS NOT FALSE AND ({where}) AND handle IS NOT NULL"
                + (" AND handle > %(after)s" if after is not None else "")
                + " GROUP BY handle ORDER BY handle LIMIT %(n)s",
                {"after": after, "n": TEXT_BATCH}).fetchall()
            if not got:
                return
            yield [(h, o) for h, oids in got for o in oids if o is not None], [h for h, _ in got]
            if len(got) < TEXT_BATCH:
                return
            after = got[-1][0]

    def put(handles: list[str], got: Mapping[str, bool]) -> None:
        # every handle of the page gets a row: one with no text is undecided (NULL)
        with write.cursor().copy("COPY cm_ai (handle, ok) FROM STDIN") as cp:
            for h in handles:
                cp.write_row((h, got.get(h)))
    # many or few: the first pages tell (at most FLAG_POOL_MIN handles held)
    parts = pages()
    head: list[Any] = []
    held = 0
    for part in parts:
        head.append(part)
        held += len(part[1])
        if held >= FLAG_POOL_MIN:
            break
    todo = itertools.chain(head, parts)
    if not head:
        pass
    elif held < FLAG_POOL_MIN:
        for pairs, handles in todo:
            put(handles, _text_verdicts(write, pairs))
    else:
        import concurrent.futures as cf
        from ontology import parent_watch
        workers = max(1, min(FLAG_WORKERS, os.cpu_count() or 1))
        info = write.info
        start = (info.dsn, getattr(info, "password", None),
                 write.execute("SHOW statement_timeout").fetchone()[0])
        with cf.ProcessPoolExecutor(workers, **parent_watch.pool_init(_text_worker, start)
                                    ) as pool:
            running: dict[Any, list[str]] = {}

            def drain(done: Any) -> None:
                for f in done:
                    put(running.pop(f), f.result())
            for pairs, handles in todo:
                running[pool.submit(_text_part, pairs)] = handles
                if len(running) >= 2 * workers:   # bounded pages in flight
                    drain(cf.wait(list(running), return_when=cf.FIRST_COMPLETED)[0])
            drain(list(cf.as_completed(list(running))))
    write.execute("ANALYZE cm_ai")


def _ai_batch(rows: Sequence[tuple[str, int, int, bytes]]) -> dict[str, bool]:
    """Per handle whose document text is in ``rows`` (``(handle, field, start, zlib
    piece)`` in field and start order): True when no field of it names Freightcom."""
    import zlib
    from integrations.image_ocr_gpu import FREIGHTCOM
    texts: dict[str, dict[int, str]] = {}
    for handle, field, start, body in rows:
        fields = texts.setdefault(handle, {})
        fields[field] = fields.get(field, "")[:start] + zlib.decompress(body).decode("utf-8")
    return {h: not any(FREIGHTCOM.search(x) for x in f.values()) for h, f in texts.items()}


def recheck(write: Any, view: str) -> dict[str, Any]:
    """The nightly re-flag (one transaction): every row still undecided (``ai_ok`` NULL: its
    body text was not indexed when it was read) has its text read again; what the refresh's
    text mark did not bring (a text written under an older field) is decided here."""
    spec = SPECS[view]
    t = f"{bv.SCHEMA}.{view}"
    with write.transaction():
        before = write.execute(f"SELECT count(*) FROM {t} WHERE ai_ok IS NULL").fetchone()[0]
        if spec.text_in_fields:
            _text_ai(write, t, "ai_ok IS NULL")
        after = write.execute(f"SELECT count(*) FROM {t} WHERE ai_ok IS NULL").fetchone()[0]
    return {"view": view, "undecided_before": before, "undecided": after}


def view_ddl(v: Any, src: str, dst: str, *, ai: bool = True) -> str:
    """``CREATE VIEW`` of a communications table's guarded view (door v2): the row's unit
    among the units the reader holds (``comms_units`` whose audience key and record marks
    the reader holds, one array per statement), Freightcom decided at build (``ai_ok``),
    the people flag of the set's current stamp and a version the set has decided (a set
    updated since the flags: its objects are read once per statement until the refresh
    flags them), no hidden id; Teams also the reader's visible chat history. ``ai`` False:
    a person's own view (``sql_views.person_schema``), the same rule without ``ai_ok``
    (Freightcom is kept from the AI only; a person may read it)."""
    from operating.pg.sql_views import SETTINGS, _setting
    spec = SPECS.get(v.name)
    aud, marks = _setting("plx.reader_audience"), _setting("plx.reader_marks")
    tag = "current_setting('plx.reader_people', true)"
    units = (f"ARRAY(SELECT u.unit FROM {src}.comms_units u WHERE "
             f"u.audience_key = ANY({aud}) AND u.marks <@ {marks})")
    covered = ("(SELECT pes.covered_ver FROM ontology.people_excluded_state pes WHERE "
               f"pes.exclusion = {tag} AND {tag} <> '')")
    current = (f"(SELECT s.notes->'marks'->'people' = jsonb_build_array(pes.exclusion, "
               "pes.built_at, coalesce(pes.updated_at, ''), pes.covered_ver) FROM "
               f"{src}.state s, ontology.people_excluded_state pes WHERE s.view = "
               f"'{v.name}')")
    hidden = ("t.object_ids[1] NOT IN (SELECT json_array_elements_text(current_setting("
              "'plx.reader_hidden', true)::json))")
    where = [f"t.unit = ANY({units})", *(["t.ai_ok"] if ai else []), "t.excluded IS FALSE",
             f"t.ver <= {covered}", f"(coalesce({current}, false) OR NOT EXISTS (SELECT 1 FROM "
             "ontology.people_excluded_objects peo WHERE peo.object_id = t.object_ids[1]))",
             "t.handle IS NOT NULL", hidden,
             *(f"current_setting('{n}', true) IS NOT NULL" for n in SETTINGS)]
    join = ""
    if spec is None or spec.scope not in ("audience", "teams", "participants"):
        where.append("false")   # an unknown rule: nothing (fail closed)
    elif spec.scope == "participants":
        # the item rule: a full reader, or one of the reader's identities a participant;
        # an unset or malformed setting reads as neither (fail closed)
        me = "nullif(current_setting('plx.reader_self', true), '')::jsonb"
        where.append(f"(coalesce(({me}->>'full') = 'true', false) OR t.participants && "
                     f"ARRAY(SELECT jsonb_array_elements_text(CASE WHEN jsonb_typeof({me}->"
                     "'ids') = 'array' THEN " + me + "->'ids' ELSE '[]'::jsonb END)))")
    elif spec.scope == "teams":
        join = (" JOIN (SELECT u.unit, e.value::timestamptz AS vf FROM jsonb_each_text("
                "current_setting('plx.reader_teams', true)::jsonb) e JOIN "
                f"{src}.comms_units u ON u.audience_key = e.key) w ON w.unit = t.unit")
        where.append(f"(w.vf = '-infinity' OR t.{spec.when} >= w.vf)")
    shown = ", ".join(f't."{c.name}"' for c in v.cols)
    return (f"CREATE VIEW {dst}.{v.name} WITH (security_barrier) AS SELECT {shown}, t.handle, "
            "t.systems, t.copies, '{}'::text[] AS withheld FROM "
            f"{src}.{v.name} t{join} WHERE " + " AND ".join(where))


# --------------------------------------------------------------------------- reader scope


def transcript_reader(runtime: Any, actor: Any) -> dict[str, Any]:
    """The reader's side of the transcript item rule (``plx.reader_self``): ``full`` when
    their full-read grant covers transcripts (``FullReadAccess.covers``, usable now), and
    ``ids`` their own source identities (object id, addresses; lower-case), the
    employee entitlement's (``item_acl_allows``). No entitlement: neither (no transcript)."""
    gate = getattr(runtime, "employee_gate", None)
    entitlement = gate.entitlement(actor) if gate is not None else None
    if entitlement is None or getattr(entitlement, "actor", None) is None:
        return {"full": False, "ids": []}
    from operating.employee_permissions import _FULL_READ_SCHEME_SCOPE
    full = getattr(entitlement, "full_read", None)
    covered = full is not None and full.covers(_FULL_READ_SCHEME_SCOPE["meeting-transcript"])
    ids = sorted({str(i).strip().lower() for i in entitlement.source_identities
                  if isinstance(i, str) and i.strip()})
    return {"full": bool(covered), "ids": ids}


def teams_windows(runtime: Any, actor: Any) -> dict[str, str]:
    """Per Teams chat unit (``teams:chat:<id>``) the reader may read at all, the earliest
    message creation its latest captured membership admits the reader to: '-infinity'
    (the whole history, messages of unknown creation too) or an instant (ISO, UTC). Decided
    by the employee's own rule at each recorded history start of the chat (a member's
    visible history only grows with a message's creation): the employee's identities in
    the chat's captured membership (``item_acl_allows``, ``capture_teams._history_acl``)
    or their full-read grant (``full_read_allows``), with the entitlement the employee
    gate resolves, as ``transcript_reader`` does (a reader that is no employee: the live
    audience, as the capture store reads for it). Which chats the reader reads at all is the
    reader's scope (the staff scope or a full reader's: ``plx.reader_audience``), which the
    guarded ``teams_messages`` view applies beside this (11-17 permissions model: staff read
    the chats they are in; the host's live audience, whose staff principals no employee
    policy grants any more, is not asked: it refused every staff reader every chat,
    2026-11-18). Empty (no Teams message) when the reader resolves to no employee, the
    host has no Teams capture, the people exclusion cannot be read or the store cannot be
    read: fail closed."""
    from integrations.capture_teams import (CHAT_KIND, DB_FILE, STORE_DIR, WHOLE_HISTORY,
                                            _history_acl, _history_start)
    gate = getattr(runtime, "employee_gate", None)
    if gate is None or getattr(runtime, "live", None) is None:
        return {}
    path = runtime.state_dir / STORE_DIR / DB_FILE
    if not path.is_file() or runtime._capture_refusal("teams-chat"):
        return {}
    entitlement = gate.entitlement(actor)
    if entitlement is None:
        # no employee (a service or fixture principal): the live audience decides, as the
        # capture store's own read does (``CaseRuntime._capture_audience``)
        audience = runtime.live.audience

        def visible(unit: str, acl: Any) -> bool:
            return bool(audience.item_visible(actor, unit, acl))
    elif getattr(entitlement, "actor", None) is None:
        return {}   # an employee refused: nothing
    else:
        from operating.case_runtime import _ITEM_ACL_STATUSES
        statuses = _ITEM_ACL_STATUSES["teams-chat"]

        def visible(unit: str, acl: Any) -> bool:
            return bool(entitlement.item_acl_allows(acl, statuses=statuses)
                        or entitlement.full_read_allows("teams-chat", acl))
    with runtime._capture_lock:
        try:
            rows = runtime._capture_read(path)._db.execute(
                "SELECT unit, acl FROM unit_acl WHERE kind = ?", (CHAT_KIND.name,)).fetchall()
        except Exception:  # noqa: BLE001 - unreadable: nothing is served
            runtime._drop_capture_read(path)
            return {}
    out: dict[str, str] = {}
    for unit, raw in rows:
        try:
            acl = json.loads(raw)
        except (TypeError, ValueError):
            continue
        starts = acl.get("history_start") if isinstance(acl, Mapping) else None
        if not isinstance(unit, str) or not isinstance(starts, Mapping):
            continue
        if visible(unit, _history_acl(acl, {"created_at": None})):
            out[unit] = "-infinity"
            continue
        moments = sorted({m for m in (_history_start(v) for v in starts.values()
                                      if isinstance(v, str)) if m and m != WHOLE_HISTORY})
        for m in moments:
            at = m.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if visible(unit, _history_acl(acl, {"created_at": at})):
                out[unit] = at
                break
    return out


def recheck_all(dsn: str) -> list[dict[str, Any]]:
    """The nightly re-flag of every built communications table (``recheck``), under the
    business views' refresh lock (never beside a refresh: that run is skipped and says
    so); a table not built yet is skipped."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        if not pg.execute("SELECT pg_try_advisory_lock(%s)", (bv.REFRESH_LOCK,)).fetchone()[0]:
            return [{"skipped": "a business views refresh is running"}]
        try:
            out = []
            for view in sorted(SPECS):
                if pg.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.{view}",)
                              ).fetchone()[0] is None:
                    out.append({"view": view, "skipped": "not built"})
                    continue
                out.append(recheck(pg, view))
            return out
        finally:
            pg.execute("SELECT pg_advisory_unlock(%s)", (bv.REFRESH_LOCK,))


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="business_comms")
    ap.add_argument("command", choices=("recheck",))
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    for got in recheck_all(args.dsn or config.dsn()):
        print(json.dumps(got, default=str))
    return 0


if __name__ == "__main__":
    import sys
    from operating.pg import business_comms as _package   # the one SPECS / VIEWS
    sys.exit(_package.main())
