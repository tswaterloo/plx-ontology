"""The attachments table (``business_comms.ATTACHMENT``): its guarded view holds the mail's
row rule, not the files'. A reader holds the attachments of the mailboxes it holds (the row's
unit), a row naming Freightcom or whose text is not read yet is withheld from the AI (never
listed on its name alone), the people exclusion and the hidden ids apply, and nothing is
listed outside a search statement's or a full reader's setting. Checked on the view's
definition (no database)."""
from __future__ import annotations

import unittest

from operating.pg import business_comms as bc
from operating.pg import business_views as bv


class AttachmentsView(unittest.TestCase):
    def ddl(self, ai):
        return bc.view_ddl(bv.VIEWS["attachments"], "biz", "biz_sql", ai=ai)

    def test_the_row_rule_is_the_mailboxes_unit_and_freightcom_is_decided_on_the_text(self):
        ai = self.ddl(True)
        self.assertIn("t.unit = ANY(", ai)
        self.assertIn("comms_units", ai)
        self.assertIn("t.ai_ok", ai)
        # no relaxation for a row with no text: an attachment whose text is not read is withheld from the AI
        self.assertNotIn("cells_ok", ai)
        self.assertIn("t.excluded IS FALSE", ai)
        self.assertIn("plx.reader_hidden", ai)
        self.assertIn("t.handle IS NOT NULL", ai)
        # the parent message's own guarded row must be readable too (its marks, hidden id,
        # exclusion and Freightcom decision), on the AI's view and a person's alike
        for ddl in (ai, self.ddl(False)):
            self.assertIn("FROM biz_sql.emails pm WHERE pm.handle = split_part(t.handle, '#attachment=', 1)", ddl)
        # a mailbox's attachments are not a drive's: no search-only setting gates them
        self.assertNotIn(bc.FILES_SETTING, ai)

    def test_a_persons_own_view_has_no_freightcom_filter_but_the_same_unit_rule(self):
        own = self.ddl(False)
        self.assertNotIn("t.ai_ok", own)
        self.assertIn("t.unit = ANY(", own)
        self.assertIn("t.excluded IS FALSE", own)

    def test_the_view_is_registered_with_its_columns_documented(self):
        v = bv.VIEWS["attachments"]
        names = [c.name for c in v.cols]
        for col in ("name", "extension", "content_type", "size", "message_ref", "mailbox",
                    "customers", "suppliers", "records"):
            self.assertIn(col, names)
        self.assertTrue(all(c.doc for c in v.cols))
        # the doc the AI reads tells it to say a count leaves out unread-text attachments
        self.assertIn("say it does not include those", v.doc)
        self.assertIs(bc.SPECS["attachments"], bc.ATTACHMENT)
        # bodies and bytes are opened by handle, never stored in the table
        self.assertNotIn("body", names)
        self.assertNotIn("text", names)


if __name__ == "__main__":
    unittest.main()
