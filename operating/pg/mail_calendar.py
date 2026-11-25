"""Read RFC 5545 iCalendar (``.ics`` / ``text/calendar``) invitation, update and
cancellation emails as structured calendar facts in ``biz.meetings`` (ASK19 finding:
only 3-5 of ~167 mailboxes have readable Graph calendars; meeting moves and invites
for everyone else arrive by mail).

Strict protocol parsing (Guardrail 2: no prose/keyword guessing):

* Unfolds RFC 5545 lines and parses ``VCALENDAR`` / ``VEVENT`` blocks from unmarked
  ``.ics`` / ``text/calendar`` attachments (``biz.attachments`` + ``ontology.text_fields``
  where ``markings = '[]'``).
* Keeps ``REQUEST``, ``PUBLISH`` and ``CANCEL`` methods; skips ``REPLY`` (attendee
  accept/decline notices never invent or overwrite meeting times).
* Reconciles per ``(mailbox, ical_uid)`` by highest ``(sequence, received_at, dtstamp)``
  so a meeting move replaces the earlier start/end/location and a cancellation sets
  ``is_cancelled = true``.
* If a ``CalendarEvent`` row already exists for ``(mailbox, ical_uid)`` and a newer mail
  update/cancellation arrived after the calendar version was recorded, updates the row
  in place (``source_kind = 'calendar_updated_by_mail'``).
* If no ``CalendarEvent`` exists for ``(mailbox, ical_uid)``, inserts a ``mail_invite``
  row carrying the source email and attachment's exact security envelope
  (``mail:<mailbox>`` audience key, union of ``rec_marks``, ``excluded``, ``ver``,
  ``object_ids``, ``handle``), so a reader sees only invitations from mail they may read."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import zlib
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("operating.pg.mail_calendar")

BUSINESS_TZ = ZoneInfo("America/New_York")

# Common Windows/Exchange TZID names found in Outlook/M365 .ics payloads -> IANA tz
WINDOWS_TZ: dict[str, str] = {
    "eastern standard time": "America/New_York",
    "eastern daylight time": "America/New_York",
    "us eastern standard time": "America/New_York",
    "(utc-05:00) eastern time (us & canada)": "America/New_York",
    "eastern time (us & canada)": "America/New_York",
    "central standard time": "America/Chicago",
    "central daylight time": "America/Chicago",
    "canada central standard time": "America/Regina",
    "(utc-06:00) central time (us & canada)": "America/Chicago",
    "mountain standard time": "America/Denver",
    "mountain daylight time": "America/Denver",
    "us mountain standard time": "America/Phoenix",
    "(utc-07:00) mountain time (us & canada)": "America/Denver",
    "pacific standard time": "America/Los_Angeles",
    "pacific daylight time": "America/Los_Angeles",
    "(utc-08:00) pacific time (us & canada)": "America/Los_Angeles",
    "atlantic standard time": "America/Halifax",
    "newfoundland standard time": "America/St_Johns",
    "utc": "UTC",
    "etc/utc": "UTC",
    "gmt": "UTC",
    "coordinated universal time": "UTC",
    "greenwich standard time": "UTC",
    "gmt standard time": "Europe/London",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "romance standard time": "Europe/Paris",
    "india standard time": "Asia/Kolkata",
    "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo",
    "aus eastern standard time": "Australia/Sydney",
}

_ALLOWED_METHODS = frozenset(("", "REQUEST", "PUBLISH", "CANCEL"))

_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W|(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_ONLINE_URL_RE = re.compile(
    r"https?://(?:teams\.microsoft\.com/l/meetup-join|teams\.live\.com/meet|"
    r"meet\.google\.com/|[a-z0-9.-]*zoom\.us/j/|[a-z0-9.-]*webex\.com/)",
    re.IGNORECASE,
)


def _tz(name: str | None) -> dt.tzinfo:
    if not name:
        return BUSINESS_TZ
    clean = name.strip().strip('"')
    mapped = WINDOWS_TZ.get(clean.lower(), clean)
    try:
        return ZoneInfo(mapped)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return BUSINESS_TZ


def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _parse_dt(value: str, params: dict[str, str]) -> tuple[dt.datetime | None, bool]:
    """Parse an RFC 5545 DATE or DATE-TIME value into ``(timestamptz, is_all_day)``."""
    raw = value.strip()
    if not raw:
        return None, False
    val_type = (params.get("VALUE") or "").upper()
    if val_type == "DATE" or (len(raw) == 8 and raw.isdigit()):
        try:
            d = dt.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            return None, False
        return dt.datetime(d.year, d.month, d.day, tzinfo=_tz(params.get("TZID"))), True
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", raw)
    if not m:
        return None, False
    y, mo, da, hh, mm, ss, z = m.groups()
    try:
        tz = dt.timezone.utc if z == "Z" else _tz(params.get("TZID"))
        return dt.datetime(int(y), int(mo), int(da), int(hh), int(mm), int(ss), tzinfo=tz), False
    except ValueError:
        return None, False


def _parse_duration(value: str) -> dt.timedelta | None:
    m = _DURATION_RE.fullmatch(value.strip())
    if not m:
        return None
    sign = -1 if m.group("sign") == "-" else 1
    try:
        delta = dt.timedelta(
            weeks=int(m.group("weeks") or 0),
            days=int(m.group("days") or 0),
            hours=int(m.group("hours") or 0),
            minutes=int(m.group("minutes") or 0),
            seconds=int(m.group("seconds") or 0),
        )
    except (ValueError, OverflowError):
        return None
    return sign * delta


def _mailto(value: str) -> str | None:
    raw = value.strip()
    if raw.lower().startswith("mailto:"):
        raw = raw[7:].strip()
    raw = raw.strip("<>\"' ").lower()
    return raw if "@" in raw and " " not in raw else None


def _unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    # RFC 5545 Section 3.2: parameter values may be DQUOTE-quoted and contain ':' or ';'.
    in_quote = False
    colon = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            colon = i
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1:]
    parts: list[str] = []
    start = 0
    in_quote = False
    for i, ch in enumerate(head):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ";" and not in_quote:
            parts.append(head[start:i])
            start = i + 1
    parts.append(head[start:])
    name = parts[0].strip().upper()
    if not name:
        return None
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value


def parse_ics(text: str) -> list[dict[str, Any]]:
    """Parse RFC 5545 ``VCALENDAR`` text into structured ``VEVENT`` dicts.

    Skips ``METHOD:REPLY`` (attendee accept/decline messages). Returns one dict per valid
    ``VEVENT`` carrying ``ical_uid`` and ``start_at``."""
    if not text or "BEGIN:VEVENT" not in text.upper():
        return []
    method = ""
    events: list[dict[str, Any]] = []
    in_event = False
    depth = 0
    cur: dict[str, Any] | None = None

    for line in _unfold(text):
        parsed = _split_prop(line)
        if parsed is None:
            continue
        name, params, value = parsed
        val_up = value.strip().upper()
        if not in_event:
            if name == "METHOD":
                method = val_up
            elif name == "BEGIN" and val_up == "VEVENT":
                in_event = True
                depth = 1
                cur = {
                    "ical_uid": None, "sequence": 0, "dtstamp": None,
                    "start_at": None, "end_at": None, "duration": None,
                    "is_all_day": False, "subject": None, "location": None,
                    "organizer": None, "attendees": [], "is_cancelled": False,
                    "is_online": False, "method": method,
                }
            continue
        if name == "BEGIN":
            depth += 1
            continue
        if name == "END":
            depth -= 1
            if depth == 0 and val_up == "VEVENT":
                in_event = False
                if cur is not None:
                    cur["method"] = method
                    if method == "CANCEL":
                        cur["is_cancelled"] = True
                    if cur["start_at"] is not None and cur["end_at"] is None and cur["duration"]:
                        try:
                            cur["end_at"] = cur["start_at"] + cur["duration"]
                        except (ValueError, OverflowError):
                            cur["end_at"] = None
                    if cur["ical_uid"] and cur["start_at"] is not None:
                        cur["attendees"] = sorted(set(cur["attendees"]))
                        events.append(cur)
                cur = None
            continue
        if depth != 1 or cur is None:
            continue
        if name == "UID":
            uid = value.strip()
            if uid:
                cur["ical_uid"] = uid
        elif name == "SEQUENCE":
            try:
                cur["sequence"] = int(value.strip())
            except ValueError:
                pass
        elif name == "DTSTAMP":
            ts, _ = _parse_dt(value, params)
            if ts is not None:
                cur["dtstamp"] = ts
        elif name == "DTSTART":
            ts, all_day = _parse_dt(value, params)
            if ts is not None:
                cur["start_at"] = ts
                cur["is_all_day"] = all_day
        elif name == "DTEND":
            ts, _ = _parse_dt(value, params)
            if ts is not None:
                cur["end_at"] = ts
        elif name == "DURATION":
            cur["duration"] = _parse_duration(value)
        elif name == "SUMMARY":
            s = _unescape(value).strip()
            if s:
                cur["subject"] = s
        elif name == "LOCATION":
            loc = _unescape(value).strip()
            if loc:
                cur["location"] = loc
                if _ONLINE_URL_RE.search(loc):
                    cur["is_online"] = True
        elif name == "DESCRIPTION":
            if _ONLINE_URL_RE.search(value):
                cur["is_online"] = True
        elif name in ("X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-GOOGLE-CONFERENCE"):
            if value.strip():
                cur["is_online"] = True
        elif name == "ORGANIZER":
            addr = _mailto(value)
            if addr:
                cur["organizer"] = addr
        elif name == "ATTENDEE":
            addr = _mailto(value)
            if addr:
                cur["attendees"].append(addr)
        elif name == "STATUS":
            if val_up == "CANCELLED":
                cur["is_cancelled"] = True

    if method not in _ALLOWED_METHODS:
        return []
    if method == "CANCEL":
        for ev in events:
            ev["method"] = method
            ev["is_cancelled"] = True
    return events


def _conn(write: Any) -> Any:
    return write if hasattr(write, "cursor") else write.connection


def _assemble(chunks: list[tuple[int, bytes]]) -> str:
    text = ""
    for start, body in chunks:
        text = text[:start] + zlib.decompress(bytes(body)).decode("utf-8", "replace")
    return text


def apply(write: Any, table: str) -> dict[str, int]:
    """Read unmarked ``.ics`` / ``text/calendar`` attachments from ``biz.attachments`` +
    ``biz.emails`` + ``ontology.text_fields`` and reconcile them into ``table``
    (``biz.meetings`` or ``biz.meetings_next``).

    Preserves exact mail security envelopes on inserted rows (``mail:<mailbox>``
    audience key, union of email + attachment ``rec_marks``, ``excluded``, ``ver``,
    ``object_ids``, ``unit``). Never updates a ``CalendarEvent`` row from a marked or
    excluded email/attachment.
    """
    from operating.pg import business_comms as bc, business_views as bv

    stats = {
        "ics_attachments": 0, "events_parsed": 0, "inserted_from_mail": 0,
        "updated_calendar_from_mail": 0, "cancelled_from_mail": 0, "mailboxes_covered": 0,
    }
    for req in ("biz.attachments", "biz.emails"):
        if write.execute("SELECT to_regclass(%s) IS NULL", (req,)).fetchone()[0]:
            return stats

    # Always reconcile mail_invite rows from scratch so deleted, newly-marked, or
    # newly-excluded invitations never linger across refreshes.
    write.execute(f"DELETE FROM {table} WHERE source_kind = 'mail_invite'")

    att_rows = write.execute(
        "SELECT a.handle, a.mailbox, a.object_ids, a.systems, a.audience_keys, "
        "a.rec_marks, a.excluded, a.ver, "
        "e.handle, e.mailbox, e.object_ids, e.systems, e.audience_keys, "
        "e.rec_marks, e.excluded, e.ver, "
        "e.received_at, e.customers, e.suppliers, e.records, fd.field_id "
        "FROM biz.attachments a "
        "JOIN biz.emails e ON e.handle = split_part(a.handle, '#attachment=', 1) "
        "JOIN ontology.text_fields fd ON fd.version_handle = a.handle "
        "AND fd.object_id = a.object_ids[1] AND fd.markings = '[]' "
        "WHERE (a.extension = 'ics' OR a.content_type ILIKE '%calendar%') "
        "AND e.handle IS NOT NULL AND a.mailbox IS NOT NULL"
    ).fetchall()
    stats["ics_attachments"] = len(att_rows)
    if not att_rows:
        return stats

    field_ids = sorted({int(r[20]) for r in att_rows})
    chunks_by_field: dict[int, list[tuple[int, bytes]]] = {}
    for lo in range(0, len(field_ids), 10_000):
        part = field_ids[lo:lo + 10_000]
        for fid, start, body in write.execute(
                "SELECT field_id, start, body FROM ontology.text_chunks "
                "WHERE field_id = ANY(%s) ORDER BY field_id, start", (part,)).fetchall():
            chunks_by_field.setdefault(int(fid), []).append((int(start), bytes(body)))

    # (mailbox, ical_uid) -> (rank_key, event_dict, mail_meta)
    best: dict[tuple[str, str], tuple[tuple, dict[str, Any], tuple]] = {}
    for row in att_rows:
        fid = int(row[20])
        chunks = chunks_by_field.get(fid)
        if not chunks:
            continue
        try:
            raw_text = _assemble(chunks)
            events = parse_ics(raw_text)
        except (zlib.error, ValueError, OverflowError):
            continue
        mailbox = str(row[1]).strip().lower()
        received_at = row[16] or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        for ev in events:
            stats["events_parsed"] += 1
            key = (mailbox, ev["ical_uid"])
            dtstamp = ev["dtstamp"] or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
            rank = (int(ev["sequence"]), received_at, dtstamp)
            prev = best.get(key)
            if prev is None or rank >= prev[0]:
                best[key] = (rank, ev, row)

    if not best:
        return stats

    # Live check against people_excluded_objects and own_issued_documents for BOTH
    # email and attachment object_ids (fail closed if attachment is excluded/own-issued).
    candidate_oids = sorted({
        oid
        for _rank, _ev, meta in best.values()
        for oid in (*(meta[2] or ()), *(meta[10] or ()))
        if oid
    })
    excluded_oids: set[str] = set()
    if candidate_oids and write.execute(
            "SELECT to_regclass('ontology.people_excluded_objects') IS NOT NULL").fetchone()[0]:
        for (oid,) in write.execute(
                "SELECT DISTINCT object_id FROM ontology.people_excluded_objects "
                "WHERE object_id = ANY(%s::text[])", (candidate_oids,)).fetchall():
            excluded_oids.add(str(oid))
    own_issued_oids: set[str] = set()
    if candidate_oids and write.execute(
            "SELECT to_regclass('ontology.own_issued_documents') IS NOT NULL").fetchone()[0]:
        for (oid,) in write.execute(
                "SELECT DISTINCT object_id FROM ontology.own_issued_documents "
                "WHERE object_id = ANY(%s::text[])", (candidate_oids,)).fetchall():
            own_issued_oids.add(str(oid))

    # Inspect existing CalendarEvent rows in ``table`` by (mailbox, ical_uid),
    # including rows already updated by mail so repeated refreshes stay idempotent.
    existing: dict[tuple[str, str], tuple[str, dt.datetime | None]] = {}
    for mb, uid, handle, rec_at in write.execute(
            f"SELECT m.mailbox, m.ical_uid, m.handle, v.recorded_at::timestamptz "
            f"FROM {table} m LEFT JOIN ontology.object_versions v "
            "ON v.object_id = m.object_ids[1] AND v.ver = m.ver "
            "WHERE m.mailbox IS NOT NULL AND m.ical_uid IS NOT NULL "
            "AND m.source_kind IN ('calendar', 'calendar_updated_by_mail')").fetchall():
        existing[(str(mb).lower(), str(uid))] = (str(handle), rec_at)

    # Map mailbox -> staff email addresses to determine is_organizer accurately
    owner_addrs: dict[str, set[str]] = {}
    if write.execute("SELECT to_regclass('biz.staff') IS NOT NULL").fetchone()[0]:
        for sid, email in write.execute(
                "SELECT lower(btrim(person_id)), lower(btrim(email)) FROM biz.staff "
                "WHERE person_id IS NOT NULL AND email IS NOT NULL").fetchall():
            owner_addrs.setdefault(sid, set()).add(email)

    bc._ensure_units(write)
    mailboxes_seen: set[str] = set()
    to_insert: list[tuple] = []

    for (mailbox, uid), (_rank, ev, meta) in sorted(best.items()):
        (a_handle, _a_mb, a_oids, a_sys, a_aud, a_marks, a_excl, a_ver,
         e_handle, e_mb, e_oids, e_sys, e_aud, e_marks, e_excl, e_ver,
         received_at, custs, supps, recs, _fid) = meta

        # Strict security envelope validation (fail closed on any mismatch or missing field)
        if not e_mb or str(e_mb).strip().lower() != mailbox:
            continue
        expected_aud = f"mail:{mailbox}"
        e_aud_list = list(e_aud or [])
        a_aud_list = list(a_aud or [])
        if len(e_aud_list) != 1 or e_aud_list[0] != expected_aud:
            continue
        if len(a_aud_list) != 1 or a_aud_list[0] != expected_aud:
            continue
        if not e_marks or not a_marks or e_ver is None or a_ver is None:
            continue
        e_oid_list = [str(o) for o in (e_oids or ()) if o]
        a_oid_list = [str(o) for o in (a_oids or ()) if o]
        if not e_oid_list or not a_oid_list:
            continue
        combined_oids = e_oid_list + [o for o in a_oid_list if o not in e_oid_list]
        if any(o in own_issued_oids for o in combined_oids):
            continue

        rec_marks = sorted(set(e_marks) | set(a_marks))
        ver = max(int(e_ver), int(a_ver))
        if e_excl is True or a_excl is True or any(o in excluded_oids for o in combined_oids):
            excluded: bool | None = True
        elif e_excl is False and a_excl is False:
            excluded = False
        else:
            excluded = None

        start_at: dt.datetime = ev["start_at"]
        start_date = start_at.astimezone(BUSINESS_TZ).date()
        end_at: dt.datetime | None = ev["end_at"]

        cal = existing.get((mailbox, uid))
        if cal is not None:
            cal_handle, cal_rec_at = cal
            # Never update a CalendarEvent row from an excluded, undecided, or marked mail
            if (excluded is False and rec_marks == ["[]"]
                    and received_at is not None
                    and (cal_rec_at is None or received_at > cal_rec_at)):
                mailboxes_seen.add(mailbox)
                if ev["is_cancelled"]:
                    stats["cancelled_from_mail"] += 1
                write.execute(
                    f"UPDATE {table} SET start_at = %s, start_date = %s, end_at = %s, "
                    "location = coalesce(%s, location), is_cancelled = %s, "
                    "source_kind = 'calendar_updated_by_mail', "
                    "ver = GREATEST(ver, %s), "
                    f"words = {bc._words(bc.MEETING)} "
                    "WHERE handle = %s AND mailbox = %s AND ical_uid = %s",
                    (start_at, start_date, end_at, ev["location"], bool(ev["is_cancelled"]),
                     ver, cal_handle, mailbox, uid))
                stats["updated_calendar_from_mail"] += 1
            continue

        mailboxes_seen.add(mailbox)
        if ev["is_cancelled"]:
            stats["cancelled_from_mail"] += 1
        combined_sys = list(dict.fromkeys([*(e_sys or ()), *(a_sys or ())]))
        cites = json.dumps({"attachment": a_handle}) if a_handle else "{}"

        write.execute(
            f"INSERT INTO {bv.SCHEMA}.comms_units (audience_key, marks) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING", (e_aud_list[0], rec_marks))
        unit = write.execute(
            f"SELECT unit FROM {bv.SCHEMA}.comms_units WHERE audience_key = %s AND marks = %s",
            (e_aud_list[0], rec_marks)).fetchone()[0]
        is_org = bool(ev["organizer"] and ev["organizer"] in owner_addrs.get(mailbox, ()))
        to_insert.append((
            start_at, start_date, end_at, ev["subject"], ev["organizer"], ev["attendees"],
            ev["location"], bool(ev["is_cancelled"]), bool(ev["is_online"]), is_org,
            bool(ev["is_all_day"]), uid, mailbox, "mail_invite",
            list(custs or []), list(supps or []), list(recs or []),
            e_handle, combined_oids, combined_sys, 1, e_aud_list, rec_marks,
            "{}", cites, ver, int(unit), True, excluded,
        ))

    if to_insert:
        for row in to_insert:
            write.execute(
                f"INSERT INTO {table} ("
                "start_at, start_date, end_at, subject, organizer, attendees, location, "
                "is_cancelled, is_online, is_organizer, is_all_day, ical_uid, mailbox, "
                "source_kind, customers, suppliers, records, handle, object_ids, systems, "
                "copies, audience_keys, rec_marks, col_marks, cites, ver, unit, ai_ok, "
                "excluded, words) VALUES ("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, "
                "ontology.sq_words(concat_ws(' ', %s::text, %s::text, "
                "array_to_string(%s::text[], ' '), %s::text)))",
                (*row, row[3], row[4], row[5], row[6]))
        stats["inserted_from_mail"] = len(to_insert)

    stats["mailboxes_covered"] = len(mailboxes_seen)
    return stats
