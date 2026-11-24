"""``X-PLX-Door`` / ``X-PLX-Session`` (SPEED-SPECS 10.9, the desktop store's wipe rules):
the door value is the session's own, moves with the door stamp or the reader and with
nothing else, is absent when any part is unknown, and a refused session says so only when it
sent credentials."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from operator_api.app import create_app
from operator_api.auth import SESSION_COOKIE, DevAuth, fixture_identity
from operator_api.door_value import DOOR_HEADER, SESSION_HEADER
from operator_api.mock import MockBackend


def client(stamp, principals=("staff:demo",)) -> TestClient:
    backend = MockBackend()
    backend.doors = SimpleNamespace(stamp=lambda: stamp[0], decide=lambda who: True)
    app = create_app(backend, DevAuth(fixture_identity("plx", list(principals)), mode="mock"),
                     secure_cookie=False)
    c = TestClient(app, base_url="http://testserver")
    assert c.post("/api/auth/dev-login", json={}).status_code == 200
    return c


class DoorHeader(unittest.TestCase):
    def test_constant_for_an_unchanged_reader_and_moves_with_the_stamp(self):
        stamp = [3180]
        c = client(stamp)
        first = c.get("/api/me").headers.get(DOOR_HEADER)
        self.assertRegex(first or "", r"^[0-9a-f]{32}$")
        for _ in range(5):   # reads and refresh cycles in between change nothing
            self.assertEqual(c.get("/api/me").headers.get(DOOR_HEADER), first)
        stamp[0] = 3181
        moved = c.get("/api/me").headers.get(DOOR_HEADER)
        self.assertNotEqual(moved, first)
        self.assertRegex(moved, r"^[0-9a-f]{32}$")

    def test_two_readers_never_share_a_value(self):
        a = client([1], ("staff:demo",)).get("/api/me").headers.get(DOOR_HEADER)
        b = client([1], ("staff:demo", "team:extra")).get("/api/me").headers.get(DOOR_HEADER)
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)

    def test_the_exclusion_rule_moves_it_and_a_unreadable_rule_omits_it(self):
        c = client([5])
        base = c.get("/api/me").headers.get(DOOR_HEADER)
        import operator_api.door_value as dv
        with mock.patch.object(dv, "_rule", (0.0, None)), \
                mock.patch("operating.pg.permissions.exclusion_rule_stamp", return_value="other-list"):
            self.assertNotEqual(c.get("/api/me").headers.get(DOOR_HEADER), base)
        with mock.patch.object(dv, "_rule", (0.0, None)), \
                mock.patch("operating.pg.permissions.exclusion_rule_stamp", side_effect=OSError):
            self.assertIsNone(c.get("/api/me").headers.get(DOOR_HEADER))

    def test_unknown_stamp_or_no_doors_means_no_header_never_a_guess(self):
        self.assertIsNone(client([None]).get("/api/me").headers.get(DOOR_HEADER))
        plain = TestClient(create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:demo"]),
                                                             mode="mock"), secure_cookie=False))
        plain.post("/api/auth/dev-login", json={})
        self.assertIsNone(plain.get("/api/me").headers.get(DOOR_HEADER))

    def test_a_refused_session_says_so_only_when_it_sent_credentials(self):
        c = client([1])
        bad = TestClient(c.app, base_url="http://testserver", cookies={SESSION_COOKIE: "x" * 20})
        refused = bad.get("/api/me")
        self.assertEqual((refused.status_code, refused.headers.get(SESSION_HEADER)), (401, "refused"))
        bare = TestClient(c.app, base_url="http://testserver").get("/api/me")
        self.assertEqual(bare.status_code, 401)
        self.assertIsNone(bare.headers.get(SESSION_HEADER))   # nobody signed in: says nothing about a person
        self.assertIsNone(bare.headers.get(DOOR_HEADER))

    def test_an_ordinary_route_403_is_not_a_refused_session(self):
        c = client([1])
        got = c.get("/api/late", headers={"X-PLX-View-As": "0" * 20})   # a non-owner asking to view as
        self.assertEqual(got.status_code, 403)
        self.assertIsNone(got.headers.get(SESSION_HEADER))
        self.assertIsNone(got.headers.get(DOOR_HEADER))   # a view-as read carries no door: nothing of it is kept


if __name__ == "__main__":
    unittest.main()
