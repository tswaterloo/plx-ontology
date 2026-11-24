"""A busy old-engine read leaves the person's panel standing and says the open items were not read."""
import types
import unittest
from datetime import date
from unittest import mock

from operator_api import team_view as tv
from operator_api.errors import UserFacing


class Unread(unittest.TestCase):
    def panel(self, brief_run):
        oid = "11111111-2222-3333-4444-555555555555"
        key = tv.key_of(oid)
        row = {"name": "Ana", "days": {}, "with": {}, "hours": [0] * 24, "roles": [],
               "reply": {}, "reply_n": {}, "reply_weeks": [], "addresses": ["ana@x.example"]}
        me = types.SimpleNamespace(
            _team_parts=lambda reader: (True, frozenset(), lambda o: False,
                                        {"window": {"to": date.today().isoformat()}},
                                        {oid: {"alerts": 2}}, {}, {oid: row}, [oid], True, {}),
            _brief_run=brief_run, store_path="x")
        with mock.patch.object(tv, "person_row", return_value={
                "key": key, "name": "Ana", "self": False, "team": None, "roles": [], "d7": {},
                "d28": {}, "awaiting_reply": 0, "tasks": 0, "alerts": 0, "alerts_closed": 0,
                "replies_28": 0, "trend": [], "hours": []}):
            return tv.TeamViewMixin.team_person(me, object(), key)

    def test_a_busy_read_is_not_a_failed_panel(self):
        def busy(reader, job, **k):
            raise UserFacing("busy", "busy", 503)
        # no open kinds are read without a store, so the read never runs: force it
        with mock.patch("operating.inbox_triage.open_rows", return_value=[{"owner": "11111111-2222-3333-4444-555555555555", "kind": "reply_owed"}]):
            with mock.patch("operating.proactive_engine.inbox_path", return_value="x"):
                got = self.panel(busy)
        self.assertTrue(got.items_unread)
        self.assertEqual(got.items, [])
        self.assertEqual(got.withheld, 0)


if __name__ == "__main__":
    unittest.main()
