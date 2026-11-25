"""Fast, reader-scoped meeting prep briefs over the Postgres door (PLAN-P2-C2(D)).

Builds a rich meeting prep brief in two bounded REPEATABLE READ READ ONLY transactions
(zero N+1 ``plx.object``/``plx.around`` loops, < 2 s target) strictly through the reader's
own Postgres door:

1. ``biz_person.*`` (inside ``scoped_read`` with the reader's ``comms`` settings:
   ``plx.reader_audience``, ``plx.reader_marks``, ``plx.reader_hidden``,
   ``plx.reader_teams_windows``, ``plx.reader_self``): reads the meeting itself,
   transcript availability, earlier meetings, recent emails and Teams threads (last
   ``THREAD_DAYS`` days) with the meeting's attendees or counterparties, and recent
   decisions/action items from ``assist.findings_feed`` INNER JOINED to the reader's own
   guarded ``emails`` / ``teams_messages`` views (so zero excluded-person or unreadable
   mailbox/chat rows can ever surface).
2. ``biz_page.*`` (inside ``scoped_read(..., person=True)`` as ``plx_page_reader``):
   reads counterparties (customers/suppliers), staff names for attendees, open & late
   purchase orders, open sales orders, open alerts/tasks involving the counterparties or
   attendees, and (only when the reader holds ``finance``) aged balances.

When ``meeting_transcripts`` has no captured transcript for the meeting/series, the
``notes`` section reports a truthful ``not_available`` state:
``"No transcript captured yet; brief built from mail, Teams, and open records."``

Guardrail 1: every list query computes the exact visible total via ``count(*) OVER ()`` so
``more`` is never silent, and every section states its explicit scope and ``as_of``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Collection, Mapping, Sequence

from operating import briefs as br

LOG = logging.getLogger(__name__)

NO_TRANSCRIPT_NOTE = (
    "No transcript captured yet; brief built from mail, Teams, and open records."
)
DOOR_SECONDS = 5.0
DOOR_LOCK_MS = 200
MAX_EMAIL_FETCH = 40
MAX_TEAMS_FETCH = 40
MAX_FINDINGS_FETCH = 30
MAX_ORDERS_FETCH = 40
MAX_ALERTS_FETCH = 40
MAX_EARLIER_FETCH = 20
SUMMARY_MAX = 220


class DoorUnavailable(RuntimeError):
    """The Postgres door or its views are not ready; caller may fall back."""


def _iso_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return br._day(value)


def _iso_str(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if isinstance(value, str) and value else None


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        d = Decimal(str(value).replace(",", "").strip())
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _clip(value: Any, n: int = br.MAX_TEXT) -> str | None:
    if value is None:
        return None
    words = " ".join(str(value).split())
    return words[:n] or None


def _clean_addrs(raw: Sequence[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or ():
        if not isinstance(item, str):
            continue
        addr = item.strip().lower()
        if addr and "@" in addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _clean_handles(raw: Sequence[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or ():
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _resolve_subject_handle(pg: Any, otype: str, object_id: str, biz_schema: str) -> str | None:
    """Map an ``object_id`` to its ``handle`` for lookup inside the guarded view.
    Never returned to the caller unless the guarded view itself admits the row."""
    with pg.transaction():
        pg.execute("SET TRANSACTION READ ONLY")
        pg.execute(f"SET LOCAL statement_timeout = {int(DOOR_SECONDS * 1000)}")
        if otype in br.MEETING_TYPES:
            row = pg.execute(
                "SELECT l1_handle FROM ontology.object_versions "
                "WHERE object_id = %s AND superseded_at IS NULL LIMIT 1",
                (object_id,),
            ).fetchone()
            return row[0] if row and isinstance(row[0], str) else None
        view = "customers" if otype == "Customer" else "suppliers"
        row = pg.execute(
            f"SELECT handle FROM {biz_schema}.lineage "
            "WHERE view = %s AND object_ids @> ARRAY[%s]::text[] "
            "ORDER BY handle LIMIT 1",
            (view, object_id),
        ).fetchone()
        return row[0] if row and isinstance(row[0], str) else None


def _built_relations(pg: Any, schema: str, names: Sequence[str]) -> set[str]:
    rows = pg.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = ANY(%s)",
        (schema, list(names)),
    ).fetchall()
    return {r[0] for r in rows}


def _has_relation(pg: Any, qualified: str) -> bool:
    row = pg.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,)).fetchone()
    return bool(row and row[0])


def _match_clause(
    customers: list[str],
    suppliers: list[str],
    addrs: list[str],
    *,
    alias: str = "",
    addr_col: str = "addresses",
) -> tuple[str, dict[str, Any]]:
    """Build a parameterized disjunction matching customers, suppliers, or addresses.
    Returns ('false', {}) when all three lists are empty."""
    pfx = f"{alias}." if alias else ""
    parts: list[str] = []
    params: dict[str, Any] = {}
    if customers:
        parts.append(f"{pfx}customers && %(customers)s::text[]")
        params["customers"] = customers
    if suppliers:
        parts.append(f"{pfx}suppliers && %(suppliers)s::text[]")
        params["suppliers"] = suppliers
    if addrs:
        parts.append(f"{pfx}{addr_col} && %(addrs)s::text[]")
        params["addrs"] = addrs
    if not parts:
        return "false", {}
    return "(" + " OR ".join(parts) + ")", params


def _read_comms_phase(
    pg: Any,
    person_schema: str,
    otype: str,
    handle: str,
    now: datetime,
    reader_addrs: frozenset[str],
) -> dict[str, Any]:
    """Transaction 1 (as store role with reader's ``comms`` settings set by ``scoped_read``):
    reads the meeting (if meeting brief), transcripts, earlier meetings, recent emails,
    recent Teams messages, and recent findings (decisions/requests/complaints) strictly
    through ``biz_person.*``."""
    built = _built_relations(
        pg, person_schema, ("meetings", "meeting_transcripts", "emails", "teams_messages")
    )
    if otype in br.MEETING_TYPES and "meetings" not in built:
        raise DoorUnavailable("meetings view is not built")

    meeting: dict[str, Any] | None = None
    customers: list[str] = [handle] if otype == "Customer" else []
    suppliers: list[str] = [handle] if otype == "Supplier" else []
    records: list[str] = []
    all_addrs: list[str] = []
    counterparty_addrs: list[str] = []
    ical_uid: str | None = None
    meeting_start: datetime | None = None

    if otype in br.MEETING_TYPES:
        row = pg.execute(
            f"SELECT subject, start_at, end_at, organizer, attendees, location, is_online, "
            f"ical_uid, customers, suppliers, records, handle "
            f"FROM {person_schema}.meetings WHERE handle = %(h)s LIMIT 1",
            {"h": handle},
        ).fetchone()
        if row is None:
            raise br.BriefRefused("not_found")
        meeting = {
            "subject": row[0],
            "start_at": row[1],
            "end_at": row[2],
            "organizer": row[3],
            "attendees": list(row[4] or ()),
            "location": row[5],
            "is_online": bool(row[6]),
            "ical_uid": row[7],
            "customers": _clean_handles(row[8]),
            "suppliers": _clean_handles(row[9]),
            "records": _clean_handles(row[10]),
            "handle": row[11],
        }
        customers = list(meeting["customers"])
        suppliers = list(meeting["suppliers"])
        records = list(meeting["records"])
        all_addrs = _clean_addrs([meeting["organizer"], *meeting["attendees"]])
        counterparty_addrs = [a for a in all_addrs if a not in reader_addrs]
        ical_uid = meeting["ical_uid"] or None
        meeting_start = meeting["start_at"] if isinstance(meeting["start_at"], datetime) else None

    since = now - timedelta(days=br.THREAD_DAYS)

    # 1. Transcripts check (strictly through biz_person.meeting_transcripts)
    transcripts: list[dict[str, Any]] = []
    has_any_transcript = False
    if "meeting_transcripts" in built:
        any_row = pg.execute(
            f"SELECT 1 FROM {person_schema}.meeting_transcripts LIMIT 1"
        ).fetchone()
        has_any_transcript = any_row is not None
        if has_any_transcript:
            t_parts: list[str] = []
            t_params: dict[str, Any] = {}
            if ical_uid:
                t_parts.append("ical_uid = %(ical_uid)s")
                t_params["ical_uid"] = ical_uid
            if customers:
                t_parts.append("customers && %(customers)s::text[]")
                t_params["customers"] = customers
            if suppliers:
                t_parts.append("suppliers && %(suppliers)s::text[]")
                t_params["suppliers"] = suppliers
            if t_parts:
                t_rows = pg.execute(
                    f"SELECT handle, subject, meeting_start, speakers, cue_count "
                    f"FROM {person_schema}.meeting_transcripts "
                    f"WHERE {' OR '.join(t_parts)} "
                    f"ORDER BY meeting_start DESC NULLS LAST LIMIT 10",
                    t_params,
                ).fetchall()
                for r in t_rows:
                    transcripts.append({
                        "handle": r[0],
                        "subject": r[1],
                        "meeting_start": r[2],
                        "speakers": list(r[3] or ()),
                        "cue_count": r[4],
                    })

    # 2. Earlier meetings with the same counterparties or attendees
    earlier_meetings: list[dict[str, Any]] = []
    earlier_total = 0
    if otype in br.MEETING_TYPES and "meetings" in built:
        e_parts: list[str] = []
        e_params: dict[str, Any] = {
            "h": handle,
            "before": meeting_start or now,
            "lim": MAX_EARLIER_FETCH,
        }
        if customers:
            e_parts.append("customers && %(customers)s::text[]")
            e_params["customers"] = customers
        if suppliers:
            e_parts.append("suppliers && %(suppliers)s::text[]")
            e_params["suppliers"] = suppliers
        if counterparty_addrs:
            e_parts.append("organizer = ANY(%(addrs)s::text[])")
            e_parts.append("attendees && %(addrs)s::text[]")
            e_params["addrs"] = counterparty_addrs
        if e_parts:
            e_rows = pg.execute(
                f"WITH matched AS ("
                f"  SELECT DISTINCT ON (COALESCE(NULLIF(ical_uid, ''), handle), start_at) "
                f"    handle, subject, start_at, organizer, attendees "
                f"  FROM {person_schema}.meetings "
                f"  WHERE handle <> %(h)s AND NOT COALESCE(is_cancelled, false) "
                f"    AND start_at < %(before)s AND ({' OR '.join(e_parts)}) "
                f"  ORDER BY COALESCE(NULLIF(ical_uid, ''), handle), start_at DESC, handle"
                f") "
                f"SELECT handle, subject, start_at, organizer, count(*) OVER () AS total_rows "
                f"FROM matched ORDER BY start_at DESC NULLS LAST LIMIT %(lim)s",
                e_params,
            ).fetchall()
            for r in e_rows:
                earlier_total = int(r[4])
                earlier_meetings.append({
                    "handle": r[0],
                    "subject": r[1],
                    "start_at": r[2],
                    "organizer": r[3],
                })

    # 3. Recent Emails (deduplicated per thread, newest first, exact visible count)
    emails: list[dict[str, Any]] = []
    emails_total = 0
    if "emails" in built:
        where_e, e_params = _match_clause(customers, suppliers, counterparty_addrs)
        if where_e != "false":
            e_params.update({"since": since, "lim": MAX_EMAIL_FETCH})
            e_rows = pg.execute(
                f"WITH matched AS ("
                f"  SELECT DISTINCT ON (COALESCE(NULLIF(thread, ''), NULLIF(message_id, ''), handle)) "
                f"    handle, subject, sender, received_at, left(said, {SUMMARY_MAX}) AS summary, "
                f"    records "
                f"  FROM {person_schema}.emails "
                f"  WHERE {where_e} AND received_at >= %(since)s "
                f"    AND NOT COALESCE(is_bulk, false) "
                f"  ORDER BY COALESCE(NULLIF(thread, ''), NULLIF(message_id, ''), handle), "
                f"    received_at DESC NULLS LAST, handle"
                f") "
                f"SELECT handle, subject, sender, received_at, summary, records, "
                f"  count(*) OVER () AS total_rows "
                f"FROM matched ORDER BY received_at DESC NULLS LAST, handle LIMIT %(lim)s",
                e_params,
            ).fetchall()
            for r in e_rows:
                emails_total = int(r[6])
                recs = _clean_handles(r[5])
                records.extend(h for h in recs if h not in records)
                emails.append({
                    "handle": r[0],
                    "subject": r[1],
                    "sender": r[2],
                    "when": r[3],
                    "summary": r[4],
                })

    # 4. Recent Teams Messages (deduplicated per chat, newest first, exact visible count)
    teams: list[dict[str, Any]] = []
    teams_total = 0
    if "teams_messages" in built:
        where_t, t_params = _match_clause(customers, suppliers, counterparty_addrs)
        if where_t != "false":
            t_params.update({"since": since, "lim": MAX_TEAMS_FETCH})
            t_rows = pg.execute(
                f"WITH matched AS ("
                f"  SELECT DISTINCT ON (COALESCE(NULLIF(chat, ''), handle)) "
                f"    handle, COALESCE(NULLIF(subject, ''), NULLIF(chat_topic, ''), 'Teams chat') AS title, "
                f"    COALESCE(NULLIF(author, ''), sender) AS who, created_at, "
                f"    left(said, {SUMMARY_MAX}) AS summary, records "
                f"  FROM {person_schema}.teams_messages "
                f"  WHERE {where_t} AND created_at >= %(since)s "
                f"  ORDER BY COALESCE(NULLIF(chat, ''), handle), created_at DESC NULLS LAST, handle"
                f") "
                f"SELECT handle, title, who, created_at, summary, records, "
                f"  count(*) OVER () AS total_rows "
                f"FROM matched ORDER BY created_at DESC NULLS LAST, handle LIMIT %(lim)s",
                t_params,
            ).fetchall()
            for r in t_rows:
                teams_total = int(r[6])
                recs = _clean_handles(r[5])
                records.extend(h for h in recs if h not in records)
                teams.append({
                    "handle": r[0],
                    "title": r[1],
                    "who": r[2],
                    "when": r[3],
                    "summary": r[4],
                })

    # 5. Recent Decisions & Action Items from assist.findings_feed gated by biz_person.*
    findings: list[dict[str, Any]] = []
    findings_total = 0
    if _has_relation(pg, "assist.findings_feed") and (
        "emails" in built or "teams_messages" in built
    ):
        union_parts: list[str] = []
        f_params: dict[str, Any] = {"since": since, "lim": MAX_FINDINGS_FETCH}
        if "emails" in built:
            w_em, p_em = _match_clause(customers, suppliers, counterparty_addrs, alias="e")
            if w_em != "false":
                f_params.update(p_em)
                union_parts.append(
                    f"SELECT f.said_at, i.item->>'kind' AS kind, i.item->>'topic' AS topic, "
                    f"  i.item->>'summary' AS summary, coalesce(i.item->>'who', '') AS who, "
                    f"  e.handle, e.subject AS title, 'email' AS channel "
                    f"FROM assist.findings_feed f "
                    f"JOIN {person_schema}.emails e ON f.source = 'emails' AND e.handle = ANY(f.handles) "
                    f"CROSS JOIN LATERAL jsonb_array_elements(f.findings) i(item) "
                    f"WHERE {w_em} AND f.said_at >= %(since)s "
                    f"  AND (i.item->>'kind') IN ('decision', 'request', 'complaint')"
                )
        if "teams_messages" in built:
            w_tm, p_tm = _match_clause(customers, suppliers, counterparty_addrs, alias="t")
            if w_tm != "false":
                f_params.update(p_tm)
                union_parts.append(
                    f"SELECT f.said_at, i.item->>'kind' AS kind, i.item->>'topic' AS topic, "
                    f"  i.item->>'summary' AS summary, coalesce(i.item->>'who', '') AS who, "
                    f"  t.handle, COALESCE(NULLIF(t.subject, ''), NULLIF(t.chat_topic, ''), 'Teams') AS title, "
                    f"  'teams' AS channel "
                    f"FROM assist.findings_feed f "
                    f"JOIN {person_schema}.teams_messages t ON f.source = 'teams_messages' "
                    f"  AND t.handle = f.handles[1] "
                    f"CROSS JOIN LATERAL jsonb_array_elements(f.findings) i(item) "
                    f"WHERE {w_tm} AND f.said_at >= %(since)s "
                    f"  AND (i.item->>'kind') IN ('decision', 'request', 'complaint')"
                )
        if union_parts:
            f_rows = pg.execute(
                f"WITH raw AS ({' UNION ALL '.join(union_parts)}), "
                f"dedup AS ("
                f"  SELECT DISTINCT ON (kind, summary) "
                f"    said_at, kind, topic, summary, who, handle, title, channel "
                f"  FROM raw ORDER BY kind, summary, said_at DESC NULLS LAST, handle"
                f") "
                f"SELECT said_at, kind, topic, summary, who, handle, title, channel, "
                f"  count(*) OVER () AS total_rows "
                f"FROM dedup ORDER BY said_at DESC NULLS LAST, handle LIMIT %(lim)s",
                f_params,
            ).fetchall()
            for r in f_rows:
                findings_total = int(r[8])
                findings.append({
                    "when": r[0],
                    "kind": r[1],
                    "topic": r[2],
                    "summary": r[3],
                    "who": r[4],
                    "handle": r[5],
                    "title": r[6],
                    "channel": r[7],
                })

    return {
        "meeting": meeting,
        "customers": customers,
        "suppliers": suppliers,
        "records": records[:100],
        "all_addrs": all_addrs,
        "counterparty_addrs": counterparty_addrs,
        "transcripts": transcripts,
        "has_any_transcript": has_any_transcript,
        "earlier": earlier_meetings,
        "earlier_total": earlier_total,
        "emails": emails,
        "emails_total": emails_total,
        "teams": teams,
        "teams_total": teams_total,
        "findings": findings,
        "findings_total": findings_total,
        "built_comms": sorted(built),
    }


def _read_business_phase(
    pg: Any,
    page_schema: str,
    otype: str,
    subject_handle: str,
    comms_data: Mapping[str, Any],
    *,
    has_finance: bool,
) -> dict[str, Any]:
    """Transaction 2 (as ``plx_page_reader`` over ``biz_page.*`` inside ``scoped_read``):
    reads parties, staff names, open/late POs, open sales orders, open alerts/tasks, and
    (when ``has_finance``) aged balances."""
    built = _built_relations(
        pg,
        page_schema,
        (
            "customers",
            "suppliers",
            "staff",
            "purchase_orders",
            "sales_orders",
            "alerts",
            "alert_runs",
            "customer_balances",
            "supplier_balances",
        ),
    )

    customers_in = list(comms_data["customers"])
    suppliers_in = list(comms_data["suppliers"])
    records_in = list(comms_data["records"])
    all_addrs = list(comms_data["all_addrs"])
    counterparty_addrs = list(comms_data["counterparty_addrs"])

    # 1. Resolve visible customers and suppliers (linked handles OR exact email match)
    if otype == "Customer" and "customers" not in built:
        raise DoorUnavailable("customers view is not built")
    if otype == "Supplier" and "suppliers" not in built:
        raise DoorUnavailable("suppliers view is not built")

    party_customers: list[dict[str, Any]] = []
    if "customers" in built and (customers_in or counterparty_addrs):
        c_parts, c_params = [], {}
        if customers_in:
            c_parts.append("handle = ANY(%(ch)s::text[])")
            c_params["ch"] = customers_in
        if counterparty_addrs:
            c_parts.append("lower(email) = ANY(%(addrs)s::text[])")
            c_params["addrs"] = counterparty_addrs
        for r in pg.execute(
            f"SELECT handle, name, chain FROM customers WHERE {' OR '.join(c_parts)} "
            f"ORDER BY name, handle LIMIT 50",
            c_params,
        ).fetchall():
            party_customers.append({"handle": r[0], "name": r[1], "chain": r[2]})
    if otype == "Customer" and not any(c["handle"] == subject_handle for c in party_customers):
        raise br.BriefRefused("not_found")

    party_suppliers: list[dict[str, Any]] = []
    if "suppliers" in built and (suppliers_in or counterparty_addrs):
        s_parts, s_params = [], {}
        if suppliers_in:
            s_parts.append("handle = ANY(%(sh)s::text[])")
            s_params["sh"] = suppliers_in
        if counterparty_addrs:
            s_parts.append("lower(email) = ANY(%(addrs)s::text[])")
            s_params["addrs"] = counterparty_addrs
        for r in pg.execute(
            f"SELECT handle, name FROM suppliers WHERE {' OR '.join(s_parts)} "
            f"ORDER BY name, handle LIMIT 50",
            s_params,
        ).fetchall():
            party_suppliers.append({"handle": r[0], "name": r[1]})
    if otype == "Supplier" and not any(s["handle"] == subject_handle for s in party_suppliers):
        raise br.BriefRefused("not_found")

    cust_handles = [c["handle"] for c in party_customers]
    supp_handles = [s["handle"] for s in party_suppliers]

    # 2. Resolve staff attendees (for display names & matching purchaser/salesperson/concerns)
    staff_by_email: dict[str, dict[str, str]] = {}
    staff_names_lower: list[str] = []
    staff_person_ids: list[str] = []
    if "staff" in built and all_addrs:
        for r in pg.execute(
            "SELECT lower(email), name, person_id FROM staff WHERE lower(email) = ANY(%(addrs)s::text[])",
            {"addrs": all_addrs},
        ).fetchall():
            if r[0]:
                staff_by_email[r[0]] = {"name": r[1] or r[0], "person_id": r[2] or ""}
            if r[1] and r[0] in counterparty_addrs:
                staff_names_lower.append(r[1].strip().lower())
            if r[2] and r[0] in counterparty_addrs:
                staff_person_ids.append(r[2].strip().lower())

    # 3. Open & Late Purchase Orders
    pos: list[dict[str, Any]] = []
    pos_total = 0
    if "purchase_orders" in built:
        po_parts, po_params = [], {"lim": MAX_ORDERS_FETCH}
        if supp_handles:
            po_parts.append("supplier_handle = ANY(%(sh)s::text[])")
            po_params["sh"] = supp_handles
        if records_in:
            po_parts.append("handle = ANY(%(recs)s::text[])")
            po_params["recs"] = records_in
        if staff_names_lower and not supp_handles and not cust_handles:
            po_parts.append("lower(purchaser) = ANY(%(staff)s::text[])")
            po_params["staff"] = staff_names_lower
        if po_parts:
            for r in pg.execute(
                "SELECT po_number, supplier, requested_date, promised_date, verdict, days_late, "
                "  total, total_currency, status, handle, withheld, count(*) OVER () AS total_rows "
                "FROM purchase_orders "
                f"WHERE status = 'open' AND ({' OR '.join(po_parts)}) "
                "ORDER BY (verdict = 'late') DESC NULLS LAST, days_late DESC NULLS LAST, "
                "  requested_date ASC NULLS LAST, po_number, handle LIMIT %(lim)s",
                po_params,
            ).fetchall():
                pos_total = int(r[11])
                pos.append({
                    "po_number": r[0],
                    "supplier": r[1],
                    "requested_date": r[2],
                    "promised_date": r[3],
                    "verdict": r[4],
                    "days_late": r[5],
                    "total": r[6],
                    "currency": r[7],
                    "status": r[8],
                    "handle": r[9],
                    "withheld": list(r[10] or ()),
                })

    # 4. Open Sales Orders
    sos: list[dict[str, Any]] = []
    sos_total = 0
    if "sales_orders" in built:
        so_parts, so_params = [], {"lim": MAX_ORDERS_FETCH}
        if cust_handles:
            so_parts.append("customer_handle = ANY(%(ch)s::text[])")
            so_params["ch"] = cust_handles
        if records_in:
            so_parts.append("handle = ANY(%(recs)s::text[])")
            so_params["recs"] = records_in
        if staff_names_lower and not supp_handles and not cust_handles:
            so_parts.append("lower(salesperson) = ANY(%(staff)s::text[])")
            so_params["staff"] = staff_names_lower
        if so_parts:
            for r in pg.execute(
                "SELECT order_number, customer, customer_po, required_date, days_late, "
                "  value, value_currency, status, handle, withheld, count(*) OVER () AS total_rows "
                "FROM sales_orders "
                f"WHERE status = 'open' AND ({' OR '.join(so_parts)}) "
                "ORDER BY days_late DESC NULLS LAST, required_date ASC NULLS LAST, "
                "  order_number, handle LIMIT %(lim)s",
                so_params,
            ).fetchall():
                sos_total = int(r[10])
                sos.append({
                    "order_number": r[0],
                    "customer": r[1],
                    "customer_po": r[2],
                    "required_date": r[3],
                    "days_late": r[4],
                    "value": r[5],
                    "currency": r[6],
                    "status": r[7],
                    "handle": r[8],
                    "withheld": list(r[9] or ()),
                })

    # 5. Open Alerts & Tasks (involving counterparties, open orders/POs, records, or attendees)
    alerts_rows: list[dict[str, Any]] = []
    alerts_total = 0
    alert_handles = _clean_handles(
        cust_handles + supp_handles + [p["handle"] for p in pos] + [s["handle"] for s in sos] + records_in
    )
    concern_keys = sorted(set(counterparty_addrs + staff_person_ids + staff_names_lower))
    if "alerts" in built and (alert_handles or concern_keys):
        a_parts, a_params = [], {"lim": MAX_ALERTS_FETCH}
        if alert_handles:
            a_parts.append("handle = ANY(%(ah)s::text[])")
            a_params["ah"] = alert_handles
        if concern_keys:
            a_parts.append("lower(concerns) = ANY(%(ck)s::text[])")
            a_params["ck"] = concern_keys
        for r in pg.execute(
            "SELECT rule, kind, record_table, handle, topic, why, since, concerns, "
            "  count(*) OVER () AS total_rows "
            "FROM alerts "
            f"WHERE state = 'open' AND ({' OR '.join(a_parts)}) "
            "ORDER BY since ASC NULLS LAST, rule, handle LIMIT %(lim)s",
            a_params,
        ).fetchall():
            alerts_total = int(r[8])
            alerts_rows.append({
                "rule": r[0],
                "kind": r[1],
                "record_table": r[2],
                "handle": r[3],
                "topic": r[4],
                "why": r[5],
                "since": r[6],
                "concerns": r[7],
            })

    # 6. Aged balances (ONLY when reader holds finance)
    balances: list[dict[str, Any]] = []
    if has_finance:
        if "customer_balances" in built and cust_handles:
            for r in pg.execute(
                "SELECT customer_handle, amount_due_currency, aging_bucket, sum(amount_due), "
                "  min(handle) FROM customer_balances "
                "WHERE customer_handle = ANY(%(ch)s::text[]) AND amount_due IS NOT NULL "
                "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3 LIMIT 30",
                {"ch": cust_handles},
            ).fetchall():
                balances.append({
                    "side": "customer",
                    "party_handle": r[0],
                    "currency": r[1],
                    "bucket": r[2],
                    "amount": r[3],
                    "handle": r[4],
                })
        if "supplier_balances" in built and supp_handles:
            for r in pg.execute(
                "SELECT supplier_handle, amount_due_currency, aging_bucket, sum(amount_due), "
                "  min(handle) FROM supplier_balances "
                "WHERE supplier_handle = ANY(%(sh)s::text[]) AND amount_due IS NOT NULL "
                "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3 LIMIT 30",
                {"sh": supp_handles},
            ).fetchall():
                balances.append({
                    "side": "supplier",
                    "party_handle": r[0],
                    "currency": r[1],
                    "bucket": r[2],
                    "amount": r[3],
                    "handle": r[4],
                })

    return {
        "customers": party_customers,
        "suppliers": party_suppliers,
        "staff_by_email": staff_by_email,
        "pos": pos,
        "pos_total": pos_total,
        "sos": sos,
        "sos_total": sos_total,
        "alerts": alerts_rows,
        "alerts_total": alerts_total,
        "balances": balances,
        "alerts_built": "alerts" in built,
    }


def _collect_handles(
    subject_handle: str,
    comms_data: Mapping[str, Any],
    biz_data: Mapping[str, Any],
) -> list[str]:
    handles: list[str] = [subject_handle]
    for group in (
        comms_data["transcripts"],
        comms_data["earlier"],
        comms_data["emails"],
        comms_data["teams"],
        comms_data["findings"],
        biz_data["customers"],
        biz_data["suppliers"],
        biz_data["pos"],
        biz_data["sos"],
        biz_data["alerts"],
        biz_data["balances"],
    ):
        for item in group:
            h = item.get("handle")
            if isinstance(h, str) and h:
                handles.append(h)
    return sorted(set(handles))


def _assemble(
    otype: str,
    object_id: str,
    subject_handle: str,
    comms_data: Mapping[str, Any],
    biz_data: Mapping[str, Any],
    id_map: Mapping[str, Mapping[str, str]],
    *,
    now: datetime,
    has_finance: bool,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    """Assemble the ``MEETING_SCHEMA`` brief dict from the reader-scoped Postgres results."""
    def oid_of(h: str | None) -> str | None:
        if not h:
            return None
        rec = id_map.get(h)
        return rec["object_id"] if rec else None

    meeting = comms_data["meeting"]
    if otype in br.MEETING_TYPES and meeting is not None:
        title = _clip(meeting["subject"]) or "(no subject)"
        when = _iso_str(meeting["start_at"])
    elif otype == "Customer" and biz_data["customers"]:
        title = _clip(biz_data["customers"][0]["name"]) or "Customer"
        when = None
    elif otype == "Supplier" and biz_data["suppliers"]:
        title = _clip(biz_data["suppliers"][0]["name"]) or "Supplier"
        when = None
    else:
        title = otype
        when = None

    party_refs: list[dict[str, Any]] = []
    for c in biz_data["customers"]:
        c_oid = oid_of(c["handle"]) or (object_id if c["handle"] == subject_handle else None)
        if c_oid:
            party_refs.append({"object_id": c_oid, "type": "Customer", "label": _clip(c["name"]) or "Customer"})
    for s in biz_data["suppliers"]:
        s_oid = oid_of(s["handle"]) or (object_id if s["handle"] == subject_handle else None)
        if s_oid:
            party_refs.append({"object_id": s_oid, "type": "Supplier", "label": _clip(s["name"]) or "Supplier"})

    staff_by_email = biz_data["staff_by_email"]

    def display_who(raw: str | None) -> str | None:
        if not raw:
            return None
        low = raw.strip().lower()
        if low in staff_by_email:
            return _clip(staff_by_email[low]["name"], 80)
        return _clip(raw, 80)

    # ---- Orders & Purchase Orders (late POs first, then open POs & open SOs)
    order_rows: list[dict[str, Any]] = []
    for po in biz_data["pos"]:
        bits: list[str] = []
        if po["supplier"]:
            bits.append(str(po["supplier"]))
        if po["verdict"] == "late" and po["days_late"]:
            bits.append(f"{po['days_late']} days past its expected date")
        elif po["verdict"] == "at_risk" and po["promised_date"]:
            bits.append(f"promised {_iso_str(po['promised_date'])}")
        else:
            bits.append(f"status {po['status'] or 'open'}")
        amt = None
        if has_finance and "total" not in po["withheld"]:
            d = _dec(po["total"])
            cur = _clip(po["currency"], 3)
            if d and cur and len(cur) == 3:
                amt = (d, cur.upper())
        order_rows.append(
            br._row(
                f"PO {_clip(po['po_number'], 60) or 'PurchaseOrder'}",
                detail="; ".join(bits),
                day=_iso_day(po["requested_date"]),
                amount=amt,
                object_id=oid_of(po["handle"]),
                sources=[po["handle"]],
            )
        )
    for so in biz_data["sos"]:
        bits = []
        if so["customer"]:
            bits.append(str(so["customer"]))
        if so["days_late"] and int(so["days_late"]) > 0:
            bits.append(f"{so['days_late']} days past its required date")
        else:
            bits.append(f"status {so['status'] or 'open'}")
        if so["customer_po"]:
            bits.append(f"PO {so['customer_po']}")
        amt = None
        if has_finance and "value" not in so["withheld"]:
            d = _dec(so["value"])
            cur = _clip(so["currency"], 3)
            if d and cur and len(cur) == 3:
                amt = (d, cur.upper())
        order_rows.append(
            br._row(
                f"Order {_clip(so['order_number'], 60) or 'SalesOrder'}",
                detail="; ".join(bits),
                day=_iso_day(so["required_date"]),
                amount=amt,
                object_id=oid_of(so["handle"]),
                sources=[so["handle"]],
            )
        )
    orders_total = max(len(order_rows), biz_data["pos_total"] + biz_data["sos_total"])
    orders_shown = order_rows[:br.MAX_ROWS]
    orders_more = max(0, orders_total - len(orders_shown))
    orders_sec = {
        "key": "orders",
        "title": "Open orders and purchase orders",
        "state": "shown" if orders_shown else "empty",
        "note": None if (orders_shown or party_refs) else (
            "No open purchase orders or sales orders matched this meeting's counterparties or attendees."
        ),
        "rows": orders_shown,
        "more": orders_more,
        "figures": [],
    }

    # ---- Open Alerts & Tasks (split into alerts, promises, quality)
    alert_rows_general: list[dict[str, Any]] = []
    promise_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    for a in biz_data["alerts"]:
        kind = str(a["kind"] or a["rule"] or "")
        if not has_finance and kind in br.FINANCE_KINDS:
            continue
        row = br._row(
            br.alert_label(kind),
            detail=_clip(a["why"], 300),
            day=_iso_day(a["since"]),
            object_id=oid_of(a["handle"]),
            sources=[a["handle"]],
        )
        if kind in br.PROMISE_KINDS:
            promise_rows.append(row)
        elif kind in br.QUALITY_KINDS:
            quality_rows.append(row)
        else:
            alert_rows_general.append(row)

    def _cap_section(key: str, title: str, rows: list[dict[str, Any]], *, note: str | None = None,
                     total: int | None = None, state_when_empty: str = "empty") -> dict[str, Any]:
        shown = rows[:br.MAX_ROWS]
        exact_total = max(len(rows), total or 0)
        more = max(0, exact_total - len(shown))
        return {
            "key": key,
            "title": title,
            "state": "shown" if shown else state_when_empty,
            "note": note,
            "rows": shown,
            "more": more,
            "figures": [],
        }

    alerts_sec = _cap_section(
        "alerts",
        "Late items, alerts and tasks",
        alert_rows_general,
        total=max(len(alert_rows_general), biz_data["alerts_total"] - len(promise_rows) - len(quality_rows)),
        note=None if biz_data["alerts_built"] else "The alerts table is not built yet.",
        state_when_empty="empty" if biz_data["alerts_built"] else "unread",
    )

    # ---- Recent Mail & Teams Threads (merged newest-first, with sender, date, concise summary, link)
    thread_items: list[tuple[str, dict[str, Any]]] = []
    for e in comms_data["emails"]:
        day = _iso_day(e["when"])
        sort_key = _iso_str(e["when"]) or ""
        thread_items.append((
            sort_key,
            br._row(
                f"Mail: {_clip(e['subject']) or '(no subject)'}",
                detail=_clip(e["summary"], SUMMARY_MAX),
                day=day,
                who=display_who(e["sender"]),
                object_id=oid_of(e["handle"]),
                sources=[e["handle"]],
            ),
        ))
    for t in comms_data["teams"]:
        day = _iso_day(t["when"])
        sort_key = _iso_str(t["when"]) or ""
        thread_items.append((
            sort_key,
            br._row(
                f"Teams: {_clip(t['title']) or 'Teams chat'}",
                detail=_clip(t["summary"], SUMMARY_MAX),
                day=day,
                who=display_who(t["who"]),
                object_id=oid_of(t["handle"]),
                sources=[t["handle"]],
            ),
        ))
    thread_items.sort(key=lambda x: x[0], reverse=True)
    thread_rows = [r for _, r in thread_items]
    threads_total = max(len(thread_rows), comms_data["emails_total"] + comms_data["teams_total"])
    scope_bits: list[str] = []
    if party_refs:
        scope_bits.append(", ".join(p["label"] for p in party_refs[:4]))
    if comms_data["counterparty_addrs"]:
        names = [display_who(a) or a for a in comms_data["counterparty_addrs"][:5]]
        scope_bits.append("attendees: " + ", ".join(names))
    thread_note = f"Mail and Teams threads from the last {br.THREAD_DAYS} days" + (
        f" ({'; '.join(scope_bits)})." if scope_bits else "."
    )
    threads_sec = _cap_section(
        "threads", "Recent mail and Teams threads", thread_rows, note=thread_note, total=threads_total
    )

    # ---- Recent Decisions & Action Items from prior threads
    decision_rows: list[dict[str, Any]] = []
    for f in comms_data["findings"]:
        kind_label = {"decision": "Decision", "request": "Action item", "complaint": "Issue"}.get(
            f["kind"], "Note"
        )
        topic = f" ({f['topic']})" if f.get("topic") else ""
        src_title = _clip(f["title"], 80) or f["channel"].capitalize()
        decision_rows.append(
            br._row(
                f"{kind_label}{topic}: {_clip(f['summary'], br.MAX_TEXT)}",
                detail=f"From {f['channel']}: {src_title}",
                day=_iso_day(f["when"]),
                who=display_who(f["who"]) if f.get("who") else None,
                object_id=oid_of(f["handle"]),
                sources=[f["handle"]],
            )
        )
    decisions_sec = _cap_section(
        "decisions",
        "Recent decisions and action items from prior threads",
        decision_rows,
        total=comms_data["findings_total"],
        note=f"Extracted from mail and Teams threads you may read in the last {br.THREAD_DAYS} days."
        if decision_rows else None,
    )

    # ---- Earlier meetings
    earlier_rows = [
        br._row(
            f"Meeting: {_clip(e['subject']) or '(no subject)'}",
            detail=f"Organised by {display_who(e['organizer'])}" if e.get("organizer") else None,
            day=_iso_day(e["start_at"]),
            object_id=oid_of(e["handle"]),
            sources=[e["handle"]],
        )
        for e in comms_data["earlier"]
    ]

    # ---- Payments & Balance (finance-marked)
    if has_finance:
        money_rows: list[dict[str, Any]] = []
        for b in biz_data["balances"]:
            d = _dec(b["amount"])
            cur = _clip(b["currency"], 3)
            if d is None or not cur or len(cur) != 3:
                continue
            bucket = str(b["bucket"] or "open")
            label = "Overdue" if bucket not in ("current", "not_due", "0") else "Balance due"
            side = "receivable" if b["side"] == "customer" else "payable"
            money_rows.append(
                br._row(
                    label,
                    detail=f"aged {side} ({bucket})",
                    amount=(d, cur.upper()),
                    object_id=oid_of(b["handle"]),
                    sources=[b["handle"]],
                )
            )
        money_sec = _cap_section("money", "Payments and balance", money_rows)
    else:
        money_sec = {
            "key": "money",
            "title": "Payments and balance",
            "state": "finance_marked",
            "note": "Finance-marked: shown only to readers who hold finance.",
            "rows": [],
            "more": 0,
            "figures": [],
        }

    promises_sec = _cap_section("promises", "Open promises", promise_rows)
    quality_sec = _cap_section("quality", "Quality issues", quality_rows)

    # ---- Last meeting: notes and action items (truthful transcript state!)
    transcript_rows = [
        br._row(
            f"Notes: {_clip(t['subject']) or 'Meeting transcript'}",
            detail=(
                f"Meeting transcript captured ({t['cue_count']} spoken lines)"
                if t.get("cue_count") else "Meeting transcript captured"
            ),
            day=_iso_day(t["meeting_start"]),
            object_id=oid_of(t["handle"]),
            sources=[t["handle"]],
        )
        for t in comms_data["transcripts"]
    ]
    if transcript_rows:
        notes_sec = _cap_section("notes", "Last meeting: notes and action items", transcript_rows)
    else:
        notes_sec = {
            "key": "notes",
            "title": "Last meeting: notes and action items",
            "state": "not_available",
            "note": NO_TRANSCRIPT_NOTE,
            "rows": [],
            "more": 0,
            "figures": [],
        }

    sections: list[dict[str, Any]] = [
        orders_sec,
        alerts_sec,
        threads_sec,
        decisions_sec,
    ]
    if otype in br.MEETING_TYPES and earlier_rows:
        sections.append(
            _cap_section(
                "earlier",
                "Earlier meetings with the people invited",
                earlier_rows,
                total=comms_data["earlier_total"],
                note="Earlier calendar meetings sharing counterparties or attendees, as your access shows them.",
            )
        )
    sections.extend([money_sec, promises_sec, quality_sec, notes_sec])
    sections.insert(0, br.talking_points(sections))

    return {
        "schema": br.MEETING_SCHEMA,
        "kind": "meeting",
        "title": title,
        "review": [],
        "subject": {"object_id": object_id, "type": otype, "label": title},
        "subject_sources": br._source(subject_handle),
        "when": when,
        "as_of": now.isoformat(timespec="seconds"),
        "parties": party_refs,
        "possible_parties": [],
        "sections": sections,
        "gaps": [],
        "budget": {
            "seconds": None,
            "records": None,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "records_read": 0,
            "stopped": None,
        },
        "basis": (
            "Read through your Postgres door, no model call: mail, Teams, and prior-thread "
            f"decisions from the last {br.THREAD_DAYS} days; open purchase orders, sales orders, "
            "and alerts involving the meeting's counterparties or attendees; finance columns "
            "shown only when you hold finance."
        ),
    }


def build(
    pg: Any,
    door: Any,
    comms: Mapping[str, str],
    object_id: str,
    *,
    now: datetime | None = None,
    reader_addrs: Collection[str] = (),
    biz_schema: str | None = None,
) -> dict[str, Any]:
    """Build a meeting prep brief for ``object_id`` on an open Postgres connection ``pg``
    strictly through the reader's door (``door`` + ``comms``).

    Raises ``BriefRefused`` when ``object_id`` is not a preparable type or is not visible
    to this reader. Raises ``DoorUnavailable`` when the door views or settings are absent.
    """
    from operating.ontology_tools_sql import Busy, Door, ToolError, scoped_read
    from operating.pg import business_views as bv, sql_views
    import psycopg

    import time as _time
    started = _time.monotonic()
    otype = br.type_of(object_id)
    if otype not in br.MEETING_TYPES + br.PARTY_TYPES:
        raise br.BriefRefused("not_preparable")
    if not comms:
        raise DoorUnavailable("comms settings not decided")

    now = now or datetime.now(timezone.utc)
    schema = biz_schema or bv.SCHEMA
    person_schema = sql_views.person_schema(schema)
    page_schema = sql_views.page_schema(schema)
    own_addrs = frozenset(a.strip().lower() for a in reader_addrs if isinstance(a, str) and a.strip())
    held = frozenset(getattr(getattr(door, "grant", None), "held", ()) or ())
    has_finance = br.FINANCE in held

    try:
        subject_handle = _resolve_subject_handle(pg, otype, object_id, schema)
        if subject_handle is None:
            raise br.BriefRefused("not_found")

        # Transaction 1: meeting, transcripts, emails, Teams, findings via biz_person.*
        comms_data = scoped_read(
            pg,
            None,
            lambda conn: _read_comms_phase(
                conn, person_schema, otype, subject_handle, now, own_addrs
            ),
            DOOR_SECONDS,
            person=True,
            as_role=False,
            extra=comms,
            lock_ms=DOOR_LOCK_MS,
        )

        # Transaction 2: business records & alerts via biz_page.* as plx_page_reader,
        # then resolve returned handles -> object_ids in the same snapshot via `after`.
        from operating.page_reads import _ids

        def _after_ids(conn: Any, biz_data: dict[str, Any]) -> dict[str, dict[str, str]]:
            handles = _collect_handles(subject_handle, comms_data, biz_data)
            if not handles:
                return {}
            out: dict[str, dict[str, str]] = {}
            for h_val, oid, o_type in conn.execute(
                "SELECT v.l1_handle, o.object_id, o.object_type FROM ontology.object_versions v "
                "JOIN ontology.objects o ON o.object_id = v.object_id "
                "WHERE v.l1_handle = ANY(%s) AND v.superseded_at IS NULL "
                "ORDER BY v.l1_handle, o.object_id",
                (handles,),
            ).fetchall():
                out.setdefault(h_val, {"object_id": oid, "object_type": o_type})
            return out

        del _ids
        biz_data, id_map = scoped_read(
            pg,
            Door(door.grant),
            lambda conn: _read_business_phase(
                conn, page_schema, otype, subject_handle, comms_data, has_finance=has_finance
            ),
            DOOR_SECONDS,
            person=True,
            as_role=True,
            lock_ms=DOOR_LOCK_MS,
            after=_after_ids,
        )
    except br.BriefRefused:
        raise
    except DoorUnavailable:
        raise
    except (Busy, ToolError) as exc:
        raise DoorUnavailable(f"{type(exc).__name__}: {exc}") from None
    except psycopg.Error as exc:
        raise DoorUnavailable(type(exc).__name__) from None

    return _assemble(
        otype,
        object_id,
        subject_handle,
        comms_data,
        biz_data,
        id_map,
        now=now,
        has_finance=has_finance,
        elapsed_seconds=_time.monotonic() - started,
    )


def build_for_reader(
    backend: Any,
    reader: Any,
    object_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Entry point for ``operator_api.briefs``: acquires the reader's decided door and a
    connection from the page pool, then runs :func:`build`."""
    from operating.page_reads import pool_for
    from operating.pg.pool import PoolTimeout

    doors = getattr(backend, "doors", None)
    if doors is None:
        raise DoorUnavailable("no doors configured")
    held = doors.get(reader) or doors.decide_now(reader)
    if held is None or len(held) < 3 or not held[2]:
        raise DoorUnavailable("reader door not ready")
    door, dsn, comms = held[0], held[1], held[2]

    # Collect the reader's own email addresses so we match counterparty/attendee threads
    # rather than every message in the reader's own mailbox.
    reader_addrs: set[str] = set()
    email = getattr(reader, "email", None)
    if isinstance(email, str) and "@" in email:
        reader_addrs.add(email.strip().lower())
    session = getattr(reader, "session", None)
    for ident in getattr(session, "identities", ()) or ():
        if isinstance(ident, str) and "@" in ident:
            reader_addrs.add(ident.strip().lower())

    try:
        with pool_for(dsn).connection() as pg:
            return build(pg, door, comms, object_id, now=now, reader_addrs=reader_addrs)
    except PoolTimeout:
        raise DoorUnavailable("pool busy") from None


__all__ = ["NO_TRANSCRIPT_NOTE", "DoorUnavailable", "build", "build_for_reader"]
