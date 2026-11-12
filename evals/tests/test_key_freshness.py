"""Stale-key detection: a key whose records or links changed after it was made is flagged."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from evals import key_freshness as kf
from evals import live_ops
from scripts import eval_key_freshness as cli

HANDLE = "api://public.supplier/%7B%22id%22%3A1%7D@g-1"
MADE = "2026-11-06T00:00:00Z"


def store(links=(), version_at="2026-11-01T00:00:00Z"):
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE object_versions(object_id, l1_handle, markings DEFAULT '[]', recorded_at, superseded_at);
    CREATE TABLE object_values(object_id, property, value, value_type, superseded_at);
    CREATE TABLE object_removals(object_id, basis, since, until);
    CREATE TABLE link_rows(link_id, link_type, from_object, to_object, tier, recorded_at, superseded_at);""")
    c.execute("INSERT INTO object_versions VALUES ('Supplier:1', ?, '[]', ?, NULL)", (HANDLE, version_at))
    c.execute("INSERT INTO object_versions VALUES ('Email:9', 'graph-mail://x@g-2', '[]', ?, NULL)", (version_at,))
    c.execute("INSERT INTO object_values VALUES ('Email:9','subject','Re: Invoice 77','text',NULL)")
    for n, at in enumerate(links):
        c.execute("INSERT INTO link_rows VALUES (?, 'Email.mentions_supplier', 'Email:9', 'Supplier:1', 'verified', ?, NULL)", (f"l{n}", at))
    return c


def none_key():
    return {"format": "plx-live-ops-key/v1", "item_id": "ho-T-1", "card": "graph-h1-none",
            "family": "graph_walk", "suite": "graph", "computed_at": MADE, "as_of": "2026-11-05",
            "facts": [{"id": "none", "kind": "absent", "value": "no emails", "accept": ["no emails"],
                       "records": [HANDLE], "allowed": ["api"]}],
            "strata": {"answer": "none", "hops": 1, "link_types": ["Email.mentions_supplier"]},
            "query": {"reads": ["Email.mentions_supplier (in)", "filter word on subject"],
                      "rule": "the walk's last set is empty"}}


ITEM = {"question": {"text": 'As of November 5, 2026, which emails whose subject mentions "invoice" of supplier X?'}}


class KeyFreshness(unittest.TestCase):
    def test_fresh_key_is_not_flagged(self):
        self.assertEqual(kf.verdict(store(), ITEM, none_key()), [])

    def test_none_key_flagged_when_a_link_appeared_after_it(self):
        reasons = kf.verdict(store(["2026-11-07T00:00:00Z"]), ITEM, none_key())
        self.assertIn("none_now_has_records", reasons)
        self.assertIn("link_newer", reasons)

    def test_word_filter_is_applied_so_a_link_to_other_records_is_not_stale_by_replay(self):
        item = {"question": {"text": 'x whose subject mentions "refund" y'}}
        self.assertNotIn("none_now_has_records", kf.verdict(store(["2026-11-07T00:00:00Z"]), item, none_key()))

    def test_record_changed_or_gone_is_flagged(self):
        self.assertIn("record_version_newer", kf.verdict(store(version_at="2026-11-08T00:00:00Z"), None, none_key()))
        c = store(); c.execute("UPDATE object_versions SET superseded_at='x' WHERE object_id='Supplier:1'")
        self.assertIn("record_not_current", kf.verdict(c, None, none_key()))

    def test_source_absence_after_the_key_is_flagged_though_the_version_is_unchanged(self):
        c = store()
        self.assertEqual(kf.verdict(c, None, none_key()), [])
        c.execute("INSERT INTO object_removals VALUES ('Supplier:1', 'missing_from_source', "
                  "'2026-11-08T00:00:00Z', NULL)")
        self.assertEqual(kf.verdict(c, None, none_key()), ["record_removed_since"])
        c.execute("UPDATE object_removals SET until = '2026-11-09T00:00:00Z'")
        self.assertEqual(kf.verdict(c, None, none_key()),
                         ["record_removed_since", "record_returned_since"])

    def test_absence_that_ended_before_the_key_is_not_a_change(self):
        c = store()
        c.execute("INSERT INTO object_removals VALUES ('Supplier:1', 'missing_from_source', "
                  "'2026-11-01T00:00:00Z', '2026-11-03T00:00:00Z')")
        self.assertEqual(kf.verdict(c, None, none_key()), [])

    def test_count_key_is_replayed_from_its_root_so_a_vanished_link_is_flagged(self):
        c = store(["2026-11-07T00:00:00Z"])
        counted = kf.revised_key(c, ITEM, none_key(), "data moved on")
        self.assertEqual(counted["query"]["start"], "Supplier:1")
        self.assertEqual(kf.verdict(c, ITEM, counted), [])
        c.execute("UPDATE link_rows SET superseded_at = '2026-11-09T00:00:00Z'")
        self.assertIn("count_changed", kf.verdict(c, ITEM, counted))
        again = kf.revised_key(c, ITEM, counted, "link gone")
        self.assertEqual(again["facts"][0]["kind"], "absent")

    def test_revised_key_is_valid_and_keeps_history(self):
        new = kf.revised_key(store(["2026-11-07T00:00:00Z"]), ITEM, none_key(), "data moved on")
        self.assertEqual(live_ops.key_problems(new), [])
        self.assertEqual(new["facts"][0]["value"], "1")
        self.assertEqual(new["revisions"][0]["was"]["kind"], "absent")

    def test_unreplayable_walk_fails_closed(self):
        with self.assertRaises(ValueError):
            kf.revised_key(store(), {"question": {"text": "no word here"}}, none_key(), "x")

    def test_cli_report_names_ids_only_and_revise_rewrites_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "keys").mkdir(); (root / "items").mkdir()
            (root / "keys/ho-T-1.json").write_text(json.dumps(none_key()))
            (root / "items/ho-T-1.json").write_text(json.dumps(ITEM))
            conn = store(["2026-11-07T00:00:00Z"])
            stale = cli.check_set(root, conn)
            self.assertEqual(list(stale), ["ho-T-1"])
            self.assertNotIn("invoice", json.dumps(stale))
            cli.revise(root, "ho-T-1", "data moved on", conn)
            self.assertEqual(oct((root / "keys/ho-T-1.json").stat().st_mode & 0o777), "0o600")
            self.assertEqual(cli.check_set(root, conn), {})


class Hard42(unittest.TestCase):
    def test_q05_key_matches_the_mirror_or_is_flagged(self):
        mirror = Path("/data/plx-state/case-runtime/source-mirror.sqlite")
        if not mirror.is_file():
            self.skipTest("no mirror on this machine")
        keys = Path(__file__).resolve().parents[1] / "hard42" / "keys.json"
        self.assertEqual(cli.check_hard42(keys, mirror), {})


if __name__ == "__main__":
    unittest.main()
