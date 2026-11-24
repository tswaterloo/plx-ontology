"""Every view the measure registry or the Ask catalog names exists (``named_views``): a
planted missing view breaches the ops check and the refresh output names it and who names it;
a schema that is not installed is ``unknown``, never ``ok``."""
from __future__ import annotations

import unittest
from unittest import mock

from operating import measures
from operating import ontology_tools_sql_catalog as catalog
from operating.pg import business_views as bv
from operating.pg import named_views as nv
from operating.pg import sql_views
from operating.tests import test_sql_views_install as _tsv


class NamesAreDefinedOnce(unittest.TestCase):
    def test_every_name_is_a_business_view_or_a_catalog_table(self):
        known = set(bv.VIEWS) | set(catalog.GENERIC) | set(catalog.COMMS_DERIVED)
        for mid, m in measures.measures().items():
            self.assertIn(m["view"], known, mid)
        self.assertLessEqual(set(catalog.view_names()), known)
        want = nv.named("biz")
        self.assertEqual(set(want), {"biz", "biz_sql"})
        # a measure over a view derived from the guarded comms views is named in the guarded
        # schema only (the standby: biz.comms_findings does not exist and never should)
        derived = [m for m in measures.measures().values() if m["view"] in catalog.COMMS_DERIVED]
        self.assertTrue(derived)
        for m in derived:
            self.assertNotIn(m["view"], want["biz"])
            self.assertIn(m["view"], want["biz_sql"])


class OnAStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._install = classmethod(_tsv.InstallOnLiveStore._install.__func__)
        _tsv.InstallOnLiveStore.setUpClass.__func__(cls)   # the store and its guarded views

    def connect_ro(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(c.close)
        return c

    def test_present_views_pass_and_a_planted_missing_one_is_named_with_who_names_it(self):
        c = self.connect_ro()
        planted = {"biz": {"customers": ["measure m1"]},
                   "biz_sql": {"customers": ["measure m1", "Ask catalog"]}}
        with mock.patch.object(nv, "named", return_value=planted):
            got = nv.alarm_checks(c)
            self.assertEqual((got[0]["name"], got[0]["state"]), ("views_named_exist", "ok"))
            self.assertIsNone(nv.report(c))
        planted["biz_sql"]["comms_findings"] = ["Ask catalog"]
        planted["biz"]["gone_view"] = ["measure m2"]
        with mock.patch.object(nv, "named", return_value=planted):
            got = nv.alarm_checks(c)[0]
            self.assertEqual((got["state"], got["value"]), ("breach", 2))
            self.assertIn("biz_sql.comms_findings (Ask catalog)", got["detail"])
            self.assertIn("biz.gone_view (measure m2)", got["detail"])
            self.assertEqual(nv.report(c), {"biz": {"gone_view": ["measure m2"]},
                                            "biz_sql": {"comms_findings": ["Ask catalog"]}})

    def test_a_guarded_schema_not_installed_is_unknown_not_ok(self):
        c = self.connect_ro()
        planted = {"biz": {"customers": ["measure m1"]}, "other_sql": {"x": ["Ask catalog"]}}
        with mock.patch.object(nv, "named", return_value=planted), \
                mock.patch.object(sql_views, "guarded_schema", return_value="other_sql"):
            got = nv.alarm_checks(c)[0]
        self.assertEqual(got["state"], "unknown")
        self.assertIn("not installed", got["detail"])

    def test_a_check_that_cannot_run_is_unknown_and_says_why(self):
        with mock.patch.object(nv, "check", side_effect=ValueError("x")):
            got = nv.alarm_checks(None)[0]
        self.assertEqual(got["state"], "unknown")
        self.assertIn("ValueError", got["detail"])


if __name__ == "__main__":
    unittest.main()
