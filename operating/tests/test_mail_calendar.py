"""ASK19 (TRAIN): Invitation and update mail read as calendar facts in ``biz.meetings``.

Evidence label: isolated-fixture (throwaway Postgres + synthetic RFC 5545 .ics payloads)."""
from __future__ import annotations

import datetime as dt
import unittest
import zlib

from integrations import graph_read
from operating.pg import business_comms as bc, business_views as bv, business_views_code as bvc
from operating.pg import mail_calendar as mc, scratch_db
from operating.pg.migrate import MIGRATIONS_DIR

INVITE_V1 = """BEGIN:VCALENDAR\r
VERSION:2.0\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:meet-vince-001@petrasoap.com\r
SEQUENCE:0\r
DTSTAMP:20261124T130000Z\r
DTSTART;TZID=America/Toronto:20261125T150000\r
DTEND;TZID=America/Toronto:20261125T160000\r
SUMMARY:Weekly Croda\\, Review\r
LOCATION:Boardroom A\r
ORGANIZER;CN=Alice:mailto:alice@petrasoap.com\r
ATTENDEE;CN=Vince:mailto:vince@petrasoap.com\r
ATTENDEE;CN=Bob:mailto:bob@petrasoap.com\r
X-MICROSOFT-SKYPETEAMSMEETINGURL:https://teams.microsoft.com/l/meetup-join/abc\r
END:VEVENT\r
END:VCALENDAR\r
"""

UPDATE_V2 = """BEGIN:VCALENDAR\r
VERSION:2.0\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:meet-vince-001@petrasoap.com\r
SEQUENCE:1\r
DTSTAMP:20261124T195000Z\r
DTSTART;TZID=Eastern Standard Time:20261125T163000\r
DTEND;TZID=Eastern Standard Time:20261125T173000\r
SUMMARY:Weekly Croda\\, Review\r
LOCATION:Room B\r
ORGANIZER:mailto:alice@petrasoap.com\r
ATTENDEE:mailto:vince@petrasoap.com\r
END:VEVENT\r
END:VCALENDAR\r
"""

CANCEL_V3 = """BEGIN:VCALENDAR\r
VERSION:2.0\r
METHOD:CANCEL\r
BEGIN:VEVENT\r
UID:meet-cancel-002@petrasoap.com\r
SEQUENCE:2\r
DTSTAMP:20261124T200000Z\r
DTSTART:20261126T140000Z\r
DURATION:PT1H\r
SUMMARY:Canceled Sync\r
STATUS:CANCELLED\r
ORGANIZER:mailto:alice@petrasoap.com\r
ATTENDEE:mailto:vince@petrasoap.com\r
END:VEVENT\r
END:VCALENDAR\r
"""

REPLY_ACCEPT = """BEGIN:VCALENDAR\r
VERSION:2.0\r
METHOD:REPLY\r
BEGIN:VEVENT\r
UID:meet-reply-003@petrasoap.com\r
SEQUENCE:0\r
DTSTART:20261126T100000Z\r
DTEND:20261126T110000Z\r
SUMMARY:Accepted: Something\r
END:VEVENT\r
END:VCALENDAR\r
"""


class ParseIcsTests(unittest.TestCase):
    def test_parses_request_update_cancel_and_skips_reply(self):
        v1 = mc.parse_ics(INVITE_V1)
        self.assertEqual(len(v1), 1)
        ev = v1[0]
        self.assertEqual(ev["ical_uid"], "meet-vince-001@petrasoap.com")
        self.assertEqual(ev["sequence"], 0)
        self.assertEqual(ev["subject"], "Weekly Croda, Review")
        self.assertEqual(ev["location"], "Boardroom A")
        self.assertEqual(ev["organizer"], "alice@petrasoap.com")
        self.assertEqual(ev["attendees"], ["bob@petrasoap.com", "vince@petrasoap.com"])
        self.assertTrue(ev["is_online"])
        self.assertFalse(ev["is_cancelled"])
        self.assertEqual(ev["start_at"].astimezone(mc.BUSINESS_TZ).hour, 15)

        v2 = mc.parse_ics(UPDATE_V2)[0]
        self.assertEqual(v2["sequence"], 1)
        self.assertEqual(v2["start_at"].astimezone(mc.BUSINESS_TZ).hour, 16)
        self.assertEqual(v2["start_at"].astimezone(mc.BUSINESS_TZ).minute, 30)

        v3 = mc.parse_ics(CANCEL_V3)[0]
        self.assertTrue(v3["is_cancelled"])
        self.assertEqual(v3["end_at"] - v3["start_at"], dt.timedelta(hours=1))

        self.assertEqual(mc.parse_ics(REPLY_ACCEPT), [])

    def test_rfc5545_edge_cases_and_malformed_inputs(self):
        # Folded lines (space & tab), quoted ':' and ';' inside parameters, Windows TZID
        folded = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:meet-fold-001@petrasoap.com\r\n"
            "SEQUENCE:3\r\n"
            'DTSTART;TZID="(UTC-05:00) Eastern Time (US & Canada)":20261125T150000\r\n'
            'DTEND;TZID="(UTC-05:00) Eastern Time (US & Canada)":20261125T160000\r\n'
            "SUMMARY:Quarterly\r\n"
            "  Budget\r\n"
            "\tReview\r\n"
            'ORGANIZER;CN="Alice: VP; Ops":mailto:alice@petrasoap.com\r\n'
            'ATTENDEE;CN="Bob: Lead":mailto:bob@petrasoap.com\r\n'
            "BEGIN:VALARM\r\n"
            "SUMMARY:Alarm Summary Should Be Ignored\r\n"
            "END:VALARM\r\n"
            "END:VEVENT\r\n"
            "METHOD:CANCEL\r\n"
            "END:VCALENDAR\r\n"
        )
        evs = mc.parse_ics(folded)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["subject"], "Quarterly BudgetReview")
        self.assertEqual(ev["organizer"], "alice@petrasoap.com")
        self.assertEqual(ev["attendees"], ["bob@petrasoap.com"])
        self.assertEqual(ev["start_at"].astimezone(mc.BUSINESS_TZ).hour, 15)
        self.assertTrue(ev["is_cancelled"])

        # Malformed TZID, overflowing DURATION, garbage text must never raise
        malformed = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:meet-bad-001@petrasoap.com\r\n"
            "DTSTART;TZID=/etc/passwd:20261125T150000\r\n"
            "DURATION:P999999999999D\r\n"
            "SEQUENCE:notanint\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        bad_evs = mc.parse_ics(malformed)
        self.assertEqual(len(bad_evs), 1)
        self.assertIsNone(bad_evs[0]["end_at"])
        self.assertEqual(bad_evs[0]["sequence"], 0)
        self.assertEqual(mc.parse_ics("not even ical"), [])

    def test_graph_read_meeting_keeps_top_level_event_message_fields(self):
        got = graph_read._meeting({
            "@odata.type": "#microsoft.graph.eventMessageRequest",
            "meetingMessageType": "meetingRequest",
            "startDateTime": {"dateTime": "2026-11-25T16:30:00", "timeZone": "Eastern Standard Time"},
            "endDateTime": {"dateTime": "2026-11-25T17:30:00", "timeZone": "Eastern Standard Time"},
            "location": {"displayName": "Room B"},
            "iCalUId": "uid-graph-1",
        })
        self.assertEqual(got["message_type"], "meetingRequest")
        self.assertEqual(got["ical_uid"], "uid-graph-1")
        self.assertEqual(got["location"]["displayName"], "Room B")


class MailCalendarApplyTests(unittest.TestCase):
    def test_reconciles_mail_invites_moves_cancellations_and_preserves_security_envelope(self):
        import psycopg
        name, dsn = scratch_db.create_db()
        try:
            with psycopg.connect(dsn, autocommit=True) as pg:
                pg.execute("CREATE SCHEMA IF NOT EXISTS ontology")
                pg.execute("CREATE SCHEMA IF NOT EXISTS biz")
                pg.execute((MIGRATIONS_DIR / "0006_ontology.sql").read_text(encoding="utf-8"))
                pg.execute((MIGRATIONS_DIR / "0040_people_excluded_objects.sql").read_text(encoding="utf-8"))
                pg.execute((MIGRATIONS_DIR / "0041_business_views.sql").read_text(encoding="utf-8"))
                # Minimal sq_words function if not already created by 0006
                pg.execute("CREATE OR REPLACE FUNCTION ontology.sq_words(text) RETURNS tsvector "
                           "LANGUAGE sql IMMUTABLE AS $$ SELECT to_tsvector('simple', coalesce($1, '')) $$")
                pg.execute("CREATE TABLE IF NOT EXISTS ontology.text_fields ("
                           "field_id bigint PRIMARY KEY, object_id text, version_handle text, "
                           "citation text, field_path text, document_kind text, sender_class text, "
                           "markings text, text_digest text)")
                pg.execute("CREATE TABLE IF NOT EXISTS ontology.text_chunks ("
                           "chunk_id bigint PRIMARY KEY, field_id bigint, start integer, body bytea)")
                cols = ", ".join(f'"{c}" {t}' for c, t in bc.columns(bv.VIEWS["meetings"]))
                pg.execute(f"CREATE TABLE biz.meetings ({cols})")
                pg.execute("CREATE TABLE biz.emails (handle text, mailbox text, object_ids text[], "
                           "systems text[], audience_keys text[], rec_marks text[], excluded boolean, "
                           "ver bigint, received_at timestamptz, customers text[], suppliers text[], "
                           "records text[])")
                pg.execute("CREATE TABLE biz.attachments (handle text, object_ids text[], "
                           "systems text[], audience_keys text[], mailbox text, extension text, "
                           "content_type text, rec_marks text[], excluded boolean, ver bigint)")
                pg.execute("CREATE TABLE biz.staff (person_id text, email text)")
                pg.execute("INSERT INTO biz.staff VALUES ('vince-id', 'vince@petrasoap.com')")

                def put_ics(fid, mb, em_h, em_oid, rec_at, ics_text, *, marks="[]",
                            e_excluded=False, a_excluded=False,
                            e_rec_marks=None, a_rec_marks=None,
                            e_aud=None, a_aud=None, e_ver=None, a_ver=None):
                    att_h = f"{em_h}#attachment=a{fid}"
                    att_oid = f"Attachment:m:{fid}"
                    erm = e_rec_marks if e_rec_marks is not None else ["[]"]
                    arm = a_rec_marks if a_rec_marks is not None else ["[]"]
                    ea = e_aud if e_aud is not None else [f"mail:{mb}"]
                    aa = a_aud if a_aud is not None else [f"mail:{mb}"]
                    ev_num = fid if e_ver is None else e_ver
                    av_num = fid if a_ver is None else a_ver
                    pg.execute(
                        "INSERT INTO biz.emails VALUES (%s, %s, %s, ARRAY['graph-mail'], %s, %s, %s, %s, %s, '{}', '{}', '{}')",
                        (em_h, mb, [em_oid], ea, erm, e_excluded, ev_num, rec_at))
                    pg.execute(
                        "INSERT INTO biz.attachments VALUES (%s, %s, ARRAY['graph-mail'], %s, %s, 'ics', 'application/ics', %s, %s, %s)",
                        (att_h, [att_oid], aa, mb, arm, a_excluded, av_num))
                    pg.execute(
                        "INSERT INTO ontology.text_fields (field_id, object_id, version_handle, citation, "
                        "field_path, document_kind, sender_class, markings, text_digest) "
                        "VALUES (%s, %s, %s, %s, 'body', 'attachment', 'internal', %s, 'd')",
                        (fid, att_oid, att_h, att_h, marks))
                    pg.execute(
                        "INSERT INTO ontology.text_chunks (chunk_id, field_id, start, body) VALUES (%s, %s, 0, %s)",
                        (fid, fid, zlib.compress(ics_text.encode("utf-8"))))

                # 1. Vince (unreadable calendar): invite at 15:00, then move update to 16:30
                put_ics(1, "vince-id", "graph-mail://vince-id/m1", "Email:v:1",
                        dt.datetime(2026, 11, 24, 13, 0, tzinfo=dt.timezone.utc), INVITE_V1)
                put_ics(2, "vince-id", "graph-mail://vince-id/m2", "Email:v:2",
                        dt.datetime(2026, 11, 24, 19, 50, tzinfo=dt.timezone.utc), UPDATE_V2,
                        e_ver=20, a_ver=25)
                # 2. Vince cancellation email
                put_ics(3, "vince-id", "graph-mail://vince-id/m3", "Email:v:3",
                        dt.datetime(2026, 11, 24, 20, 0, tzinfo=dt.timezone.utc), CANCEL_V3)
                # 3. Marked text field must be withheld (never parsed)
                put_ics(4, "vince-id", "graph-mail://vince-id/m4", "Email:v:4",
                        dt.datetime(2026, 11, 24, 20, 5, tzinfo=dt.timezone.utc),
                        INVITE_V1.replace("meet-vince-001", "meet-marked-004"), marks='["hr"]')
                # 4. Excluded attachment (even if email not flagged) -> excluded=True
                put_ics(5, "vince-id", "graph-mail://vince-id/m5", "Email:v:5",
                        dt.datetime(2026, 11, 24, 20, 10, tzinfo=dt.timezone.utc),
                        INVITE_V1.replace("meet-vince-001", "meet-excl-005"),
                        e_excluded=False, a_excluded=True)
                # 5. Union of email + attachment rec_marks
                put_ics(7, "vince-id", "graph-mail://vince-id/m7", "Email:v:7",
                        dt.datetime(2026, 11, 24, 20, 15, tzinfo=dt.timezone.utc),
                        INVITE_V1.replace("meet-vince-001", "meet-union-007"),
                        e_rec_marks=["[]", '["pii"]'], a_rec_marks=["[]", '["hr"]'])
                # 6. Mismatched audience_keys -> fail closed (skipped)
                put_ics(8, "vince-id", "graph-mail://vince-id/m8", "Email:v:8",
                        dt.datetime(2026, 11, 24, 20, 20, tzinfo=dt.timezone.utc),
                        INVITE_V1.replace("meet-vince-001", "meet-badaud-008"),
                        e_aud=["mail:other-id"], a_aud=["mail:vince-id"])

                pg.execute("ALTER TABLE ontology.object_versions ADD COLUMN IF NOT EXISTS ver bigint")
                # 7. Existing CalendarEvent for Bob updated by a newer clean mail move
                pg.execute(
                    "INSERT INTO ontology.objects (object_id, object_type, binding_id, native_key, "
                    "audience_key, first_recorded_at) VALUES ('CalendarEvent:m:1', 'CalendarEvent', "
                    "'meetings.calendar_events', 'e1', 'meetings:calendar:bob-id', '2026-11-24T14:00:00Z')")
                pg.execute(
                    "INSERT INTO ontology.object_versions (ver, object_id, l1_handle, digest, "
                    "lineage, markings, recorded_at, superseded_at) VALUES (100, 'CalendarEvent:m:1', "
                    "'calendar-event://bob-id/e1', 'd', 'meetings', '[]', '2026-11-24T14:00:00Z', NULL)")
                pg.execute(
                    "INSERT INTO biz.meetings (start_at, start_date, end_at, subject, organizer, attendees, "
                    "location, is_cancelled, is_online, is_organizer, is_all_day, ical_uid, mailbox, "
                    "source_kind, customers, suppliers, records, handle, object_ids, systems, copies, "
                    "audience_keys, rec_marks, col_marks, cites, ver, unit, ai_ok, excluded, words) VALUES ("
                    "'2026-11-25 15:00:00-05', '2026-11-25', '2026-11-25 16:00:00-05', 'Weekly Croda, Review', "
                    "'alice@petrasoap.com', ARRAY['bob@petrasoap.com'], 'Boardroom A', false, true, false, false, "
                    "'meet-vince-001@petrasoap.com', 'bob-id', 'calendar', '{}', '{}', '{}', "
                    "'calendar-event://bob-id/e1', ARRAY['CalendarEvent:m:1'], ARRAY['calendar-event'], 1, "
                    "ARRAY['meetings:calendar:bob-id'], ARRAY['[]'], '{}'::jsonb, '{}'::jsonb, 100, 1, true, false, "
                    "to_tsvector('simple', 'weekly croda'))")
                put_ics(6, "bob-id", "graph-mail://bob-id/m6", "Email:b:6",
                        dt.datetime(2026, 11, 24, 19, 55, tzinfo=dt.timezone.utc), UPDATE_V2,
                        e_ver=110, a_ver=115)

                # 8. Existing CalendarEvent for Carol MUST NOT be updated by an excluded mail move
                pg.execute(
                    "INSERT INTO ontology.objects (object_id, object_type, binding_id, native_key, "
                    "audience_key, first_recorded_at) VALUES ('CalendarEvent:m:2', 'CalendarEvent', "
                    "'meetings.calendar_events', 'e2', 'meetings:calendar:carol-id', '2026-11-24T14:00:00Z')")
                pg.execute(
                    "INSERT INTO ontology.object_versions (ver, object_id, l1_handle, digest, "
                    "lineage, markings, recorded_at, superseded_at) VALUES (101, 'CalendarEvent:m:2', "
                    "'calendar-event://carol-id/e2', 'd', 'meetings', '[]', '2026-11-24T14:00:00Z', NULL)")
                pg.execute(
                    "INSERT INTO biz.meetings (start_at, start_date, end_at, subject, organizer, attendees, "
                    "location, is_cancelled, is_online, is_organizer, is_all_day, ical_uid, mailbox, "
                    "source_kind, customers, suppliers, records, handle, object_ids, systems, copies, "
                    "audience_keys, rec_marks, col_marks, cites, ver, unit, ai_ok, excluded, words) VALUES ("
                    "'2026-11-25 15:00:00-05', '2026-11-25', '2026-11-25 16:00:00-05', 'Carol Sync', "
                    "'alice@petrasoap.com', ARRAY['carol@petrasoap.com'], 'Boardroom A', false, true, false, false, "
                    "'meet-vince-001@petrasoap.com', 'carol-id', 'calendar', '{}', '{}', '{}', "
                    "'calendar-event://carol-id/e2', ARRAY['CalendarEvent:m:2'], ARRAY['calendar-event'], 1, "
                    "ARRAY['meetings:calendar:carol-id'], ARRAY['[]'], '{}'::jsonb, '{}'::jsonb, 101, 1, true, false, "
                    "to_tsvector('simple', 'carol sync'))")
                put_ics(9, "carol-id", "graph-mail://carol-id/m9", "Email:c:9",
                        dt.datetime(2026, 11, 24, 19, 55, tzinfo=dt.timezone.utc), UPDATE_V2,
                        e_excluded=True)

                got = mc.apply(pg, "biz.meetings")
                self.assertEqual(got["inserted_from_mail"], 4)          # m2, m3, m5 (excl), m7 (union marks)
                self.assertEqual(got["updated_calendar_from_mail"], 1)  # bob's calendar updated; carol untouched
                self.assertEqual(got["cancelled_from_mail"], 1)

                rows = {
                    (r[0], r[1]): r[2:]
                    for r in pg.execute(
                        "SELECT mailbox, ical_uid, start_at, location, is_cancelled, source_kind, "
                        "audience_keys, rec_marks, excluded, ver, object_ids, cites FROM biz.meetings"
                    ).fetchall()
                }
                # Vince's moved meeting shows 16:30 ET in Room B with mail:vince-id door + both object_ids + max ver
                v_moved = rows[("vince-id", "meet-vince-001@petrasoap.com")]
                self.assertEqual(v_moved[0].astimezone(mc.BUSINESS_TZ).hour, 16)
                self.assertEqual(v_moved[0].astimezone(mc.BUSINESS_TZ).minute, 30)
                self.assertEqual(v_moved[1], "Room B")
                self.assertFalse(v_moved[2])
                self.assertEqual(v_moved[3], "mail_invite")
                self.assertEqual(v_moved[4], ["mail:vince-id"])
                self.assertEqual(v_moved[5], ["[]"])
                self.assertFalse(v_moved[6])
                self.assertEqual(v_moved[7], 25)
                self.assertEqual(v_moved[8], ["Email:v:2", "Attachment:m:2"])
                self.assertEqual(v_moved[9], {"attachment": "graph-mail://vince-id/m2#attachment=a2"})
                # Vince's cancelled meeting
                self.assertTrue(rows[("vince-id", "meet-cancel-002@petrasoap.com")][2])
                # Marked text field withheld
                self.assertNotIn(("vince-id", "meet-marked-004@petrasoap.com"), rows)
                # Mismatched audience_keys withheld
                self.assertNotIn(("vince-id", "meet-badaud-008@petrasoap.com"), rows)
                # Excluded attachment retains excluded=True
                self.assertTrue(rows[("vince-id", "meet-excl-005@petrasoap.com")][6])
                # Union of email + attachment rec_marks
                self.assertEqual(
                    set(rows[("vince-id", "meet-union-007@petrasoap.com")][5]),
                    {"[]", '["hr"]', '["pii"]'},
                )
                # Bob's calendar event updated by the newer clean move mail + ver bumped
                b_upd = rows[("bob-id", "meet-vince-001@petrasoap.com")]
                self.assertEqual(b_upd[0].astimezone(mc.BUSINESS_TZ).hour, 16)
                self.assertEqual(b_upd[3], "calendar_updated_by_mail")
                self.assertEqual(b_upd[7], 115)
                # Carol's calendar event NOT updated by excluded mail
                c_cal = rows[("carol-id", "meet-vince-001@petrasoap.com")]
                self.assertEqual(c_cal[0].astimezone(mc.BUSINESS_TZ).hour, 15)
                self.assertEqual(c_cal[1], "Boardroom A")
                self.assertEqual(c_cal[3], "calendar")

                # Re-running apply() is idempotent (no duplicate Bob row) and drops stale mail_invite rows
                pg.execute("UPDATE ontology.text_fields SET markings = '[\"hr\"]' WHERE field_id = 3")
                mc.apply(pg, "biz.meetings")
                bob_count = pg.execute(
                    "SELECT count(*) FROM biz.meetings WHERE mailbox = 'bob-id' "
                    "AND ical_uid = 'meet-vince-001@petrasoap.com'"
                ).fetchone()[0]
                self.assertEqual(bob_count, 1)
                cancel_gone = pg.execute(
                    "SELECT count(*) FROM biz.meetings WHERE mailbox = 'vince-id' "
                    "AND ical_uid = 'meet-cancel-002@petrasoap.com'"
                ).fetchone()[0]
                self.assertEqual(cancel_gone, 0)

                # Exact comms view fingerprints: only meetings changed vs origin/main
                self.assertEqual(bvc.fingerprint(bv.VIEWS["meetings"]), "v1:b512330f32e40b7efd61667666fed")
                self.assertEqual(bvc.fingerprint(bv.VIEWS["emails"]), "v1:29f575965c75c898ee5893b1e193a")
                self.assertEqual(bvc.fingerprint(bv.VIEWS["attachments"]), "v1:f926da071d1733af776f038d5870a")
                self.assertEqual(bvc.fingerprint(bv.VIEWS["teams_messages"]), "v1:2c2c0f1eadf2f53318d572c1c7a5e")
                self.assertEqual(bvc.fingerprint(bv.VIEWS["sharepoint_files"]), "v1:4743f442f7326b9fd1b4e7c0f953c")
                self.assertEqual(bvc.fingerprint(bv.VIEWS["meeting_transcripts"]), "v1:10a011481443b441c92ce605e1df3")
        finally:
            scratch_db.drop_db(name)


if __name__ == "__main__":
    unittest.main()
