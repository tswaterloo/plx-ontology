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

    def test_the_view_is_registered_with_its_columns_documented(self):
        v = bv.VIEWS["attachments"]
        names = [c.name for c in v.cols]
        for col in ("name", "extension", "content_type", "size", "message_ref", "mailbox",
                    "customers", "suppliers", "records"):
            self.assertIn(col, names)
        self.assertTrue(all(c.doc for c in v.cols))
        # every captured attachment is a row, images and scans too (11-23: 43,003 png and
        # 18,552 jpg rows in a 5% sample of the served view): the doc the AI reads must not
        # tell it a count leaves them out
        self.assertNotIn("leaves out", v.doc)
        self.assertIs(bc.SPECS["attachments"], bc.ATTACHMENT)
        # bodies and bytes are opened by handle, never stored in the table
        self.assertNotIn("body", names)
        self.assertNotIn("text", names)


if __name__ == "__main__":
    unittest.main()
