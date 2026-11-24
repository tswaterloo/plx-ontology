"""A search whose door ran out of time says so: it never falls through to a second, slower search."""
import types
import unittest
from unittest import mock

from operator_api import models as m, page_sql
from operator_api.backend_serving import ServingMixin
from operator_api.errors import UserFacing


def fake():
    return types.SimpleNamespace(
        _pg_suggest=mock.Mock(return_value=[m.Suggestion(
            object_id="x", type="Customer", label="Fallback", match="exact", score=1.0,
            open_url="/x")]),
        reads=mock.Mock())


class SuggestBusy(unittest.TestCase):
    def test_a_timeout_or_busy_door_is_a_503_and_the_slow_search_is_not_run(self):
        for code in ("timeout", "busy"):
            me = fake()
            with mock.patch.object(page_sql, "on", return_value=True), \
                    mock.patch.object(page_sql, "search", side_effect=page_sql.PageDoorUnavailable(code)):
                with self.assertRaises(UserFacing) as got:
                    ServingMixin._suggest(me, object(), "croda", 8, person=True)
            self.assertEqual(got.exception.status, 503)
            me._pg_suggest.assert_not_called()

    def test_an_unbuilt_door_still_falls_back(self):
        me = fake()
        with mock.patch.object(page_sql, "on", return_value=True), \
                mock.patch.object(page_sql, "search", side_effect=page_sql.PageDoorUnavailable("not_ready")):
            out = ServingMixin._suggest(me, object(), "croda", 8, person=True)
        self.assertEqual(out.suggestions[0].label, "Fallback")


if __name__ == "__main__":
    unittest.main()
