"""``emails.said``: what a message itself says, from its body preview (11-22: Ask listed a
thread's mails, read a reply's quoted history and never the answer above it). Run on a
scratch Postgres: the column is one SQL expression the build and every refresh write."""
from __future__ import annotations

import unittest

try:
    import psycopg
except ImportError:   # pragma: no cover
    psycopg = None

from operating.pg import business_comms as bc
from operating.pg import mail_said as ms
from operating.pg import business_views as bv
from operating.tests import pg_testdb

SIG = ("Thank you,\r\nRoss Pennino​​\r\nPetra Hygienic Systems\r\ne: ross@example-plx.com"
       "<mailto:ross@example-plx.com>\r\n______________________________________________\r\n"
       "This email and any files transmitted with it are confidential")
CASES = (
    # a reply: its answer, the signature's rule line and what follows it gone
    ("Hi Paula,\r\n\r\nYes all received!\r\n\r\n" + SIG,
     "Hi Paula, Yes all received! Thank you, Ross Pennino Petra Hygienic Systems "
     "e: ross@example-plx.com"),
    # the mail service's first-contact safety tip is not the sender's
    ("You don't often get email from care@vendor.example. Learn why this is important\r\n\r\n"
     "Hello Ross,\r\n\r\nVery happy to hear that!",
     "Hello Ross, Very happy to hear that!"),
    ("Some people who received this message don’t often get email from a@b.example. "
     "Learn why this is important\nGood morning, attached is the revised CofA.",
     "Good morning, attached is the revised CofA."),
    # a forward with a note: the note; the Outlook header block and below are history
    ("Hi Team,\nI am going to mail the below check today.\n\n\nFrom: Ross Pennino "
     "<ross@example-plx.com>\nSent: Thursday, November 5, 2026 11:12 AM\nTo: AP",
     "Hi Team, I am going to mail the below check today."),
    # a bare forward adds nothing; a preview cut inside the From line is history too
    ("From: Ross Pennino <ross@example-plx.com>\nSent: Thursday, November 5\nTo: x", None),
    ("I have the check with me now.\n\nFrom: BJ Gottlieb <bj@vendor.example",
     "I have the check with me now."),
    ("Sounds good.\n\nOn Tue, Nov 3, 2026 at 9:14 AM Jane <jane@vendor.example> wrote:\n> ok",
     "Sounds good."),
    ("Confirmed for Friday.\n-----Original Message-----\nFrom: a", "Confirmed for Friday."),
    ("Shipped today.\n> when will it ship?", "Shipped today."),
    # links and invisible padding go; words around them stay
    ("See www.vendor.example<https://safelinks.example/?url=abc&data=05%7C02> for "
     "details͏‌ ­͏", "See www.vendor.example for details"),
    ("   \r\n ", None),
    (None, None),
)


@unittest.skipIf(psycopg is None, "no psycopg")
class SaidOnPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.dsn = pg_testdb.create_db()

    @classmethod
    def tearDownClass(cls) -> None:
        pg_testdb.drop_db(cls.db)

    def test_what_each_message_itself_says(self):
        with psycopg.connect(self.dsn) as pg:
            for preview, said in CASES:
                with self.subTest(preview=preview):
                    got = pg.execute("SELECT " + ms.said_sql("%(p)s::text"),
                                     {"p": preview}).fetchone()[0]
                    self.assertEqual(got, said)

    def test_it_is_a_documented_column_the_rows_are_written_with(self):
        # the column the readers see, documented, and the row read writes it from the
        # preview (the build and the refresh share ``_rows_sql``)
        cols = {c.name: c for c in bv.VIEWS["emails"].cols}
        self.assertEqual(cols["said"].kind, "text")
        self.assertIn("quoted history", cols["said"].doc)
        self.assertIn(ms.said_sql("p.body_preview"), bc.EMAIL.select)
        self.assertIn("body_preview", bc.EMAIL.props)
        names = [c.name for c in bv.VIEWS["emails"].cols]
        self.assertEqual(names[:len(bc.EMAIL_COLS)], [c[0] for c in bc.EMAIL_COLS])


if __name__ == "__main__":
    unittest.main()
