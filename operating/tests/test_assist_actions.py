"""The action log (``operating.assist_actions``): rows from the one definition of an act (stand-in
providers and system activity), the template/scheduled send marked ``not_human``, and the load on
Postgres: a message's handle, source and thread resolved from the comms views by record id in one
statement, a repeated file adding nothing, the fill rates said per kind (a column no row can hold
reads NULL, not a made-up share), and statements constant for 1/10/60 rows.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operating import assist_actions as aa
from operating import day_coverage as dc
from operating.tests import test_assist_matters as base

NOW = base.NOW


class Built(unittest.TestCase):
    def test_every_act_is_one_row_and_a_template_send_is_not_human(self):
        def sends(st, people, who, since, until):
            return [{"person": "p-ann", "at": f"2026-11-2{d}T14:00:00Z", "kind": "mail_sent",
                     "conversation": "t1", "to": ["buyer@x.example"], "records": ["PO:1"],
                     "object_id": f"Email:{d}", "body_key": "same", "subject_key": "s"}
                    for d in range(1, 5)] + [{"person": "p-ann", "at": "2026-11-22T10:00:00Z",
                                              "kind": "teams_sent", "conversation": "c9",
                                              "to": [], "records": [], "object_id": "Teams:1"}]
        with mock.patch.object(dc, "system_actions", lambda *a, **k: (
                [{"person": "p-bob", "at": "2026-11-22T09:00:00Z", "kind": "sage_ar_invoice",
                  "workflow": "ar_invoice_entry", "conversation": None, "to": [],
                  "records": ["Inv:1"], "system": "sage"}], __import__("collections").Counter(
                      {("sage", "x", "no_who_field"): 3}))):
            built = aa.build(None, None, {"p-ann", "p-bob"}, NOW - dt.timedelta(days=9), NOW,
                             providers=[sends])
        rows = built["rows"]
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({r["action_key"] for r in rows}), 6)
        mails = [r for r in rows if r["kind"] == "mail_sent"]
        # one body at the same minute on 4 days: a template or a scheduled send, never a person's
        self.assertTrue(all(r["not_human"] in ("system_generated", "scheduled_send") for r in mails))
        sage = next(r for r in rows if r["kind"] == "sage_ar_invoice")
        self.assertEqual((sage["system"], sage["attributed_by"], sage["not_human"]),
                         ("sage", "store_who_field", ""))
        self.assertEqual(built["lost"], {"sage|x|no_who_field": 3})
        out = Path(tempfile.mkdtemp()) / "a.jsonl"
        self.assertEqual(aa.write(out, built), 6)
        self.assertEqual(len(out.read_text().splitlines()), 6)


class Loaded(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail

    def setUp(self):
        base.Matters.setUp(self)
        for t in ("emails", "teams_messages"):      # the door's columns the real views carry
            self.pg.execute(f"ALTER TABLE biz.{t} ADD COLUMN IF NOT EXISTS ai_ok boolean DEFAULT true, "
                            "ADD COLUMN IF NOT EXISTS excluded boolean DEFAULT false, "
                            "ADD COLUMN IF NOT EXISTS ver integer DEFAULT 0")
        self.pg.execute("CREATE TABLE IF NOT EXISTS ontology.people_excluded_objects (object_id text)")

    def rows(self, n, handle_for=None):
        return [{"action_key": f"Email:o:{i}", "person": "p-ann", "at": "2026-11-22T10:00:00Z",
                 "system": "mail", "kind": "mail_sent", "object_id": f"o:{i + 1}",
                 "thread_key": f"th{i}", "recipients": ["buyer@x.example"], "records": [],
                 "attributed_by": "sender", "not_human": ""} for i in range(n)]

    def file(self, rows):
        path = Path(tempfile.mkdtemp()) / "a.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return path

    def test_a_sent_message_gets_its_handle_source_and_thread_from_the_comms_views(self):
        h = self.mail("conv-1", "ann@ours.example", ["buyer@x.example"], at=NOW)   # object_ids o:1
        path = self.file(self.rows(1) + [{**self.rows(1)[0], "action_key": "k-sys", "kind":
                                           "sage_ar_invoice", "system": "sage", "object_id": "",
                                           "thread_key": None}])
        got = aa.load(self.pg, path, covered=0)
        self.assertEqual(got, {"status": "ok", "rows": 2, "added": 2, "dropped": {}})
        mail = self.pg.execute("SELECT target_handle, source, message_key, thread_key, "
                               "recipients, has_attachment FROM assist.actions WHERE kind = "
                               "'mail_sent'").fetchone()
        self.assertEqual(mail, (h, "emails", h, "conv-1", ["buyer@x.example"], None))
        sage = self.pg.execute("SELECT target_handle, source, thread_key FROM assist.actions "
                               "WHERE kind = 'sage_ar_invoice'").fetchone()
        self.assertEqual(sage, ("", None, None))
        self.assertEqual(aa.load(self.pg, path, covered=0)["added"], 0)   # a repeat adds nothing

    def test_a_message_the_readings_door_refuses_is_dropped_with_a_typed_reason(self):
        self.mail("c-ok", "ann@ours.example", ["x@x.example"], at=NOW)               # o:1 passes
        self.mail("c-ex", "ann@ours.example", ["x@x.example"], at=NOW)               # o:2 excluded
        self.mail("c-ai", "ann@ours.example", ["x@x.example"], at=NOW)               # o:3 not ai_ok
        self.mail("c-ver", "ann@ours.example", ["x@x.example"], at=NOW)              # o:4 ver too new
        self.mail("c-obj", "ann@ours.example", ["x@x.example"], at=NOW)              # o:5 object excluded
        self.pg.execute("UPDATE biz.emails SET excluded = true WHERE handle LIKE '%c-ex%'")
        self.pg.execute("UPDATE biz.emails SET ai_ok = false WHERE handle LIKE '%c-ai%'")
        self.pg.execute("UPDATE biz.emails SET ver = 9 WHERE handle LIKE '%c-ver%'")
        self.pg.execute("INSERT INTO ontology.people_excluded_objects VALUES ('o:5')")
        rows = self.rows(6)            # o:1..o:6; o:6 is in no view
        got = aa.load(self.pg, self.file(rows), covered=0)
        self.assertEqual((got["added"], got["dropped"]), (1, {"not_readable": 4, "message_not_served": 1}))
        self.assertEqual(self.pg.execute("SELECT count(*) FROM assist.actions").fetchone()[0], 1)
        # the exclusion undecided: nothing is loaded, said
        blocked = aa.load(self.pg, self.file(rows), covered=None)
        self.assertEqual((blocked["status"], blocked["added"]), ("blocked", 0))

    def test_the_fill_rates_say_what_rows_hold_and_null_columns_read_null(self):
        for i in range(2):
            self.mail(f"conv-{i}", "ann@ours.example", ["x@x.example"], at=NOW)
        aa.load(self.pg, self.file(self.rows(2)), covered=0)
        fill = aa.fill(self.pg)
        self.assertEqual(len(fill), 1)
        f = fill[0]
        self.assertEqual((f["kind"], f["rows"]), ("mail_sent", 2))
        self.assertEqual(float(f["recipients"]), 1.0)
        self.assertEqual(float(f["has_attachment"]), 0.0)            # not held by any row yet
        self.assertEqual(float(f["thread_key"]), 1.0)

    def test_statements_do_not_grow_with_the_rows(self):
        seen = {}
        for n in (1, 10, 60):
            self.pg.execute("TRUNCATE assist.actions, biz.emails")
            self.n = 0
            for i in range(n):
                self.mail(f"c{n}-{i}", "ann@ours.example", ["x@x.example"], at=NOW)
            counting = base.Counting(self.pg)
            aa.load(counting, self.file(self.rows(n)), covered=0)
            seen[n] = counting.n
            self.assertEqual(self.pg.execute("SELECT count(*) FROM assist.actions").fetchone()[0], n)
        self.assertEqual(len(set(seen.values())), 1, seen)


if __name__ == "__main__":
    unittest.main()
