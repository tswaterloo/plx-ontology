"""Home's one-round-trip bundle (/api/home/bundle): each part is what its own route answers this
reader (same body, same status), a failed part carries its own error and the others stand, the
bundle is for a signed-in reader only, and a reader never gets a part its own route refuses."""
from __future__ import annotations

import unittest
from unittest import mock

from operator_api.errors import UserFacing
from operator_api.tests.test_app_api import mock_client, signed_in

ROUTES = {"home": "/api/home", "sidebar": "/api/sidebar", "briefing": "/api/briefing?pages=true",
          "late": "/api/late?offset=0&limit=100", "suggestions": "/api/suggestions",
          "replies": "/api/replies", "approvals": "/api/approvals"}


class HomeBundle(unittest.TestCase):
    def test_every_part_is_its_routes_own_answer(self):
        client = signed_in()
        got = client.get("/api/home/bundle")
        self.assertEqual(got.status_code, 200, got.text)
        parts = got.json()["parts"]
        self.assertEqual(sorted(parts), sorted(ROUTES))
        for name, path in ROUTES.items():
            own = client.get(path)
            self.assertEqual(parts[name]["status"], own.status_code, name)
            self.assertEqual(parts[name]["body"], own.json(), name)

    def test_a_failed_part_carries_its_own_error_and_the_others_stand(self):
        client = signed_in()
        target = "operator_api.mock.MockBackend.suggestions"
        with mock.patch(target, side_effect=UserFacing("busy", "Busy.", 503)):
            parts = client.get("/api/home/bundle").json()["parts"]
        self.assertEqual(parts["suggestions"], {"status": 503, "body": {"error": {
            "code": "busy", "message": "Busy."}}})
        self.assertTrue(all(p["status"] == 200 for n, p in parts.items() if n != "suggestions"))
        with mock.patch("operator_api.mock.MockBackend.sidebar", side_effect=RuntimeError("x")):
            parts = client.get("/api/home/bundle").json()["parts"]
        self.assertEqual(parts["sidebar"]["status"], 500)
        self.assertEqual(parts["sidebar"]["body"]["error"]["code"], "internal")
        self.assertNotIn("RuntimeError", str(parts["sidebar"]))   # never a stack or a type name
        self.assertEqual(parts["home"]["status"], 200)

    def test_signed_out_gets_nothing(self):
        got = mock_client().get("/api/home/bundle")
        self.assertEqual(got.status_code, 401)
        self.assertNotIn("parts", got.text)


if __name__ == "__main__":
    unittest.main()
