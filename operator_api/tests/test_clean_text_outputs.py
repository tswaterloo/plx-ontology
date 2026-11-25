"""Live content and garbled text hygiene across backend formatters (page_core, home_needs,
page_sql, page_parts, briefing_view, briefs, meeting_briefs, team_view).

Every test case is grounded in garbled patterns observed in production data: Teams JSON
sender blobs, GUIDs, meeting join boilerplate, URL query soup, HTML entities/tags, email
reply headers, Zendesk reply markers, FileMaker CR line breaks, doubled PO/approval prefixes,
raw snake_case enums/slugs, raw CamelCase ontology types, float residue, and em dashes.
Evidence label: isolated-fixture."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import types
import unittest
from unittest import mock

from operator_api import (
    briefing_view as bv,
    briefs as ob,
    home_needs as hn,
    meeting_briefs as mb,
    models as m,
    page_core as pc,
    page_parts as pp,
    page_sql as ps,
    team_view as tv,
)

NOW = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)


class TextHygieneCore(unittest.TestCase):
    def test_clean_text_strips_html_reply_headers_boilerplate_em_dashes_and_noise(self):
        email = (
            "##- Please type your reply above this line -## "
            "Hi Maria &amp; team,&nbsp;<br><style>.x{color:red}</style>"
            "<at id='0'>Laura Abate</at> [cid:image001.png@01DB] "
            "Please substitute PO PO100112 — lost 2085.06 CAD on 1 open order(s) (23 %). "
            "https://petrasoap.sharepoint.com/sites/RD/Shared%20Documents/Forms/AllItems.aspx"
            "?q=soapbox&sortField=Modified&isAscending=false&viewid=8edc5a08-69ff-4510-aa6e "
            "From: Alice <a@x.com> Sent: Nov 24, 2026 9:00 AM To: Bob Subject: Old thread"
        )
        got = pc.clean_text(email, strip_email_noise=True)
        self.assertIn("Hi Maria & team,", got)
        self.assertIn("Laura Abate", got)
        self.assertIn("PO100112 - lost 2,085.06 CAD on 1 open order (23%)", got)
        for bad in (
            "##-",
            "&amp;",
            "&nbsp;",
            "<style>",
            "<at",
            "cid:",
            "PO PO",
            "\u2014",
            "(s)",
            "23 %",
            "sharepoint.com/sites",
            "From: Alice",
            "Old thread",
        ):
            self.assertNotIn(bad, got, got)

    def test_clean_text_handles_github_footer_camel_counts_guids_and_word_clipping(self):
        self.assertEqual(
            pc.clean_text(
                "#897 was added to the merge queue. — Reply to this email directly, "
                "view it on GitHub, or unsubscribe. Download it today!",
                strip_email_noise=True,
            ),
            "#897 was added to the merge queue.",
        )
        self.assertEqual(
            pc.clean_text(
                "A message waits on your reply (258 PurchaseOrder named, 1 SalesOrder named)"
            ),
            "A message waits on your reply (258 purchase orders named, 1 sales order named)",
        )
        self.assertEqual(
            pc.clean_text(
                "b52e3565-9862-4f79-b42a-2acbbd81c6b9 is waiting on a reply"
            ),
            "Someone is waiting on a reply",
        )
        self.assertEqual(
            pc.clean_text('glenn@petrasoap.com wrote "Fw:" on Oct 08'),
            "glenn@petrasoap.com wrote (no subject) on Oct 08",
        )
        self.assertEqual(
            pc.clean_text("Mission Control question on "),
            "Mission Control question",
        )
        clipped = pc.clean_text("The quick brown fox jumps over the lazy dog", max_len=24)
        self.assertEqual(clipped, "The quick brown fox...")
        self.assertFalse(clipped.endswith("jum..."))

    def test_clean_person_decodes_teams_json_and_strips_raw_guids(self):
        self.assertEqual(
            pc.clean_person(
                '{"display_name":"Allison Tam","id":"29bb5cc6-b067-440d-a75b-ea94b5aca3ed","kind":"user"}'
            ),
            "Allison Tam",
        )
        self.assertIsNone(
            pc.clean_person('{"display_name":"","id":"c191003b7a59492fb88695e7e563f195","kind":"user"}')
        )
        self.assertIsNone(pc.clean_person("63d8d890-05f2-4a46-a29b-9c7990d7b0d7"))
        self.assertIsNone(pc.clean_person("c_5k649pp2ip56jhsf7juhfghmgs@group.calendar.google.com"))
        self.assertEqual(
            pc.clean_person("586kwbglljgtdpl+eb764c5c-33cf-4b6d-a63a-72d1423afbbc@marketplace.amazon.com"),
            "Amazon Marketplace",
        )
        self.assertEqual(pc.clean_person("@blulabs.com"), "blulabs.com")

    def test_clean_subject_and_location(self):
        self.assertEqual(
            pc.clean_subject("Synced invitation: PLX / Gleamin @ (EST) (vince@petrasoap.com)"),
            "PLX / Gleamin",
        )
        self.assertEqual(
            pc.clean_subject("Re:Re: RE: PO 5353\xa0---Approval Needed"),
            "Re: PO 5353 ---Approval Needed",
        )
        self.assertEqual(pc.clean_subject("Fw: ", fallback="(no subject)"), "(no subject)")
        self.assertEqual(
            pc.clean_subject("LASIK MD\u2014Link to join"),
            "LASIK MD - Link to join",
        )
        self.assertEqual(
            pc.clean_location(
                "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NTYx%40thread.v2/0?context=%7b%22Tid%22%3a%221%22%7d"
            ),
            "Microsoft Teams",
        )
        self.assertEqual(
            pc.clean_location("Petra Lab-X, 90 Moyal Court; https://meet.google.com/voh-fnwn-hwo"),
            "Petra Lab-X, 90 Moyal Court (Google Meet)",
        )

    def test_clean_enum_alert_label_and_money(self):
        self.assertEqual(pc.clean_enum("RAW_MATERIAL", "item_type"), "Raw material")
        self.assertEqual(pc.clean_enum("FINAL_PRODUCT", "item_type"), "Finished good")
        self.assertEqual(pc.clean_enum("true", "boolean"), "Yes")
        self.assertEqual(pc.clean_enum("false", "boolean"), "No")
        self.assertEqual(pc.clean_enum("tax_payroll", "payee_class"), "Tax and payroll")
        self.assertEqual(pc.clean_enum("open", "status"), "Open")
        self.assertEqual(pc.clean_alert_label("price_below_typical"), "Price below typical")
        self.assertEqual(pc.clean_alert_label("open_quote_aging"), "Aging open quote")
        self.assertEqual(pc.clean_alert_label("unconfirmed_po"), "Unconfirmed purchase order")
        self.assertEqual(pc.clean_alert_label("custom_new_slug"), "Custom new slug")
        self.assertEqual(pc.clean_money_amount("100"), "100")
        self.assertEqual(pc.clean_money_amount("112.50"), "112.50")
        self.assertEqual(pc.clean_money_amount("1.77456712280625", max_decimals=4), "1.7746")
        self.assertEqual(pc.clean_money_amount("100531.7558784", max_decimals=2), "100531.76")
        self.assertEqual(pc.clean_money_amount("12345.67890000", max_decimals=4), "12345.6789")
        self.assertEqual(pc.join_clean(["Acme", "ACME", None, "", "N/A", "CAD"]), "Acme · CAD")


class FormattersEndToEnd(unittest.TestCase):
    def test_home_needs_cleans_teams_json_sender_trailing_prep_em_dash_and_double_po(self):
        matter = {
            "item_id": "m1",
            "kind_of_matter": "conversation",
            "state": "open",
            "channel": "teams",
            "sender": '{"display_name":"Keenan Bittner","id":"b52e3565-9862-4f79-b42a-2acbbd81c6b9","kind":"user"}',
            "subject": "QC Morning Headcount Logged \u2014 2026-11-24",
            "summary": "PO PO0025776 update",
            "note": "review and update today's headcount",
            "ask": "",
            "message": "##- Please type your reply above this line -## Can you confirm PO PO0025776?",
            "next_act": "reply",
            "basis_at": NOW,
            "sources_detail": {},
        }
        got = hn._matter_item(matter)
        self.assertEqual(got.person, "Keenan Bittner")
        self.assertEqual(got.title, "QC Morning Headcount Logged - 2026-11-24")
        self.assertEqual(got.line, "Review and update today's headcount")
        self.assertEqual(got.quote, "Can you confirm PO0025776?")
        self.assertNotIn("\u2014", got.title + got.line + (got.quote or ""))

        late = hn._late_item(
            m.LatePo(
                object_id="PurchaseOrder:portal:1",
                label="PO PO100587",
                supplier="Croda\rCanada",
                due_date="2026-11-01",
                days_late=24,
                reason="Needed by 2026-11-01 \u2014 still open",
                open_url="/order?kind=purchase_order&h=1",
            )
        )
        self.assertEqual(late.title, "PO100587")
        self.assertEqual(late.person, "Croda Canada")
        self.assertNotIn("\u2014", late.line)

    def test_page_sql_cleans_po_prefix_alerts_enums_and_search_dedupe(self):
        lp = ps.late_po(
            {"handle": "api://public.app_purchase_order/1@g", "po_number": "PO100587",
             "supplier": "Croda\rLtd", "requested_date": "2026-11-01", "days_late": 5,
             "total": Decimal("100531.7558784"), "total_currency": "CAD"},
            {},
        )
        self.assertEqual(lp.label, "PO100587")
        self.assertEqual(lp.supplier, "Croda Ltd")
        self.assertEqual(lp.amount.amount, "100531.76")

        alert = ps.alert_item(
            {"rule": "negative_margin", "kind": "margin_loss", "handle": "api://x/1@g",
             "subject": "1HOUR\u2014AFTER", "record_table": "sales_orders",
             "why": "Order OR1 lost 2085.06 CAD on PO PO100112 \u2014 check cost."},
            {},
            1,
            None,
        )
        self.assertEqual(alert.subject_label, "1HOUR - AFTER")
        self.assertEqual(alert.summary, "Order OR1 lost 2,085.06 CAD on PO100112 - check cost.")
        self.assertEqual(ps.alert_label({"rule": "unconfirmed_po"}), "Unconfirmed purchase order")

        row = {
            "kind": "sales_order", "handle": "api://x/2@g", "title": "SO-10",
            "context": "Acme", "code": "PO-99", "status": "open", "rank": 1,
        }
        with mock.patch.object(ps, "read", return_value=({"find": [row]}, {})):
            hits = ps.search(object(), None, "SO-10", 5)
        self.assertEqual(hits[0].sublabel, "Acme · PO-99 · Open")

    def test_briefs_and_upcoming_strip_meeting_boilerplate_and_clean_rows(self):
        entry = ob.upcoming_entry(
            {
                "object_id": "CalendarEvent:x:1",
                "label": "Synced invitation: PLX / Gleamin @ (EST) (vince@petrasoap.com)",
                "location": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_1%40thread.v2/0?context=%7b%7d",
                "agenda": "Hi Team, postpone to Wed. ________________________________ Microsoft Teams meeting Join: https://teams.microsoft.com/meet/123 Meeting ID: 123",
                "organizer": {"name": "c_abc@group.calendar.google.com", "address": "c_abc@group.calendar.google.com", "role": "organizer"},
                "attendees": [],
                "parties": [],
                "sources": [],
            }
        )
        self.assertEqual(entry.object.label, "PLX / Gleamin")
        self.assertEqual(entry.location, "Microsoft Teams")
        self.assertEqual(entry.agenda, "Hi Team, postpone to Wed.")

        brief = ob.brief_model(
            {
                "kind": "meeting",
                "title": "LASIK MD\u2014Sync",
                "as_of": "2026-11-25T12:00:00Z",
                "subject": {"object_id": "CalendarEvent:x:1", "label": "LASIK MD\u2014Sync"},
                "parties": [],
                "gaps": [],
                "basis": "Built cleanly.",
                "sections": [
                    {
                        "key": "orders",
                        "title": "Orders",
                        "state": "shown",
                        "more": 0,
                        "figures": [],
                        "rows": [
                            {"label": "PO PO100587", "detail": "Croda; status open", "sources": []},
                            {"label": "PO PurchaseOrder", "detail": "aged receivable (not_due)", "sources": []},
                            {
                                "label": "Mail: Re:Re: RE: Update",
                                "detail": "##- Please type your reply above this line -## Ready to ship.",
                                "who": '{"display_name":"Allison Tam","id":"29bb5cc6-b067-440d-a75b-ea94b5aca3ed","kind":"user"}',
                                "sources": [],
                            },
                        ],
                    }
                ],
            }
        )
        self.assertEqual(brief.title, "LASIK MD - Sync")
        rows = brief.sections[0].rows
        self.assertEqual((rows[0].label, rows[0].detail), ("PO100587", "Croda; Open"))
        self.assertEqual((rows[1].label, rows[1].detail), ("Purchase order", "Aged receivable (not yet due)"))
        self.assertEqual((rows[2].label, rows[2].detail, rows[2].who), ("Mail: Re: Update", "Ready to ship.", "Allison Tam"))

        notes = mb.note_rows([{"text": "Follow up \u2014 ask for COA", "at": "2026-11-25", "records": ["CalendarEvent:x:1"]}], {"CalendarEvent:x:1"})
        self.assertEqual(notes[0]["label"], "Follow up - ask for COA")

    def test_briefing_view_team_view_and_page_parts_clean_outputs(self):
        bi = bv.briefing_item(
            {
                "rank": 1,
                "id": "g1",
                "event_type": "price_below_typical",
                "label": "price_below_typical",
                "object_id": "SalesOrder:sage:1",
                "object_type": "SalesOrder",
                "summary": "PO PO100112 lost 2085.06 CAD \u2014 2 PurchaseOrder named",
                "next_action": {"text": "Check PO PO100112.", "rule": "check"},
            },
            {},
            None,
            counterpart='{"display_name":"Maria Hooper","id":"2761a13a-5651-4b28-a12c-d78bf2e1aee0","kind":"user"}',
        )
        self.assertEqual(bi.label, "Price below typical")
        self.assertEqual(bi.subject_label, "Sales order record")
        self.assertEqual(bi.counterpart, "Maria Hooper")
        self.assertEqual(bi.summary, "PO100112 lost 2,085.06 CAD - 2 purchase orders named")

        # team_view deduplicates CSV departments and cleans drill-down items
        row = tv.person_row(
            "11111111-2222-4333-8444-555555555555",
            {"name": "Pat\u2014Mercer", "department": "Sales, Sales, Purchasing", "days": {}},
            until=date(2026, 11, 25),
            team=None,
            open_={},
            is_self=False,
        )
        self.assertEqual(row["name"], "Pat - Mercer")
        self.assertEqual(row["team"], "Sales, Purchasing")

        # page_parts avoids duplicating record name in timeline and cleans unknown impact kinds
        self.assertEqual(pp.impact_change_words({"kind": "supplier_shutdown"}), "Supplier shutdown")
        page = m.ObjectPage.model_construct(
            object_id="SalesOrder:s:1",
            timeline=[m.TimelineEvent(at="2026-11-25", system="Sage", title="Ordered: GRANITE CLUB")],
        )
        pp.label_timeline(
            types.SimpleNamespace(_call=lambda *a: {}),
            page,
            {"Customer:s:1": "GRANITE CLUB"},
            {"GRANITE CLUB": "open"},
            [{"objects": [{"object_id": "SalesOrder:s:1"}, {"object_id": "Customer:s:1"}]}],
        )
        self.assertEqual(page.timeline[0].title, "Ordered: GRANITE CLUB")
        self.assertEqual(page.timeline[0].summary, "Status: Open")


if __name__ == "__main__":
    unittest.main()
