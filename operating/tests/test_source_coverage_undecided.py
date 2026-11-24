import unittest

from operating.pg import source_coverage as sc


def _report(undecided):
    return {"findings": [], "types": [], "undecided": undecided}


class UndecidedAlarm(unittest.TestCase):
    def test_absolute_count_alarms_without_any_baseline(self):
        got = sc.alarming(_report({"attachments": {"rows": 3_000_000, "undecided": 21_815}}), None)
        self.assertEqual([a["kind"] for a in got], ["undecided_text"])
        self.assertEqual(got[0]["undecided"], 21_815)

    def test_small_table_share_alarms_and_a_clean_table_does_not(self):
        got = sc.alarming(_report({"events": {"rows": 400, "undecided": 10},
                                   "emails": {"rows": 3_000_000, "undecided": 12}}), None)
        self.assertEqual([a["view"] for a in got], ["events"])

    def test_report_without_the_field_still_reads(self):
        self.assertEqual(sc.alarming({"findings": [], "types": []}, None), [])


if __name__ == "__main__":
    unittest.main()
