"""Unit tests for fast Suggestions trail loading (precomputed ``biz.plan_section_items``
first + bounded live fallback <= 3 items / 3s) and normalized-name supplier email
fallback in ``operator_api.backend.WorkspaceBackend``."""
from __future__ import annotations

import contextlib
import json
import unittest
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from operator_api import backend as be


class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakePg:
    def __init__(self, handlers):
        self._handlers = handlers

    def connection(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        for needle, result in self._handlers:
            if needle in sql:
                return _FakeCur(result)
        return _FakeCur([(False,)])


class _FakeView:
    class access:  # noqa: N801
        @staticmethod
        def grant():
            return (lambda _a: True), frozenset({"ops"})

        @staticmethod
        def decide(handles):
            return {h: None for h in handles}

    @contextlib.contextmanager
    def _request(self):
        yield


class SuggestionsFastBackendTests(unittest.TestCase):
    def test_norm_supplier_name_matches_duplicate_supplier_email(self) -> None:
        self.assertEqual(
            be.WorkspaceBackend._norm_supplier_name("Nordic Oils, Inc."),
            be.WorkspaceBackend._norm_supplier_name("Nordic Oils"),
        )
        guard = PeopleExclusion(["00000000-0000-4000-8000-0000000000e1"])
        guard.attach_directory([
            ("00000000-0000-4000-8000-0000000000e1", "gone@synth.example", ["gone@synth.example"]),
            ("00000000-0000-4000-8000-0000000000c1", "alex@synth.example", ["alex@synth.example"]),
        ])
        pg = _FakePg([
            ("to_regclass('biz.purchase_orders')", [(False,)]),
            ("to_regclass('biz.suppliers')", [(False,)]),
            ("FROM ontology.objects o", [
                ("PurchaseOrder:bc:100", "Nordic Oils Inc.", "bc://sup/1", None),
            ]),
            ("SELECT handle, name, email FROM biz.suppliers", [
                ("bc://sup/1", "Nordic Oils Inc.", None),
                ("portal://sup/99", "Nordic Oils", "orders@nordic.example"),
            ]),
        ])
        host = type("Host", (), {
            "pg_dsn": "postgresql:///synth",
            "pg": lambda self_: pg,
            "_norm_supplier_name": staticmethod(be.WorkspaceBackend._norm_supplier_name),
        })()
        with mock.patch("operator_api.feedback.exclusion", return_value=guard):
            got = be.WorkspaceBackend._supplier_contacts_for_pos(
                host, _FakeView(), ["PurchaseOrder:bc:100"],
            )
        self.assertEqual(got["PurchaseOrder:bc:100"]["address"], "orders@nordic.example")
        self.assertIn("matching supplier record", got["PurchaseOrder:bc:100"]["basis"])

    def test_precomputed_po_trails_reads_plan_section_items_even_when_over_24h_old(self) -> None:
        raw_trail = {
            "subject": "PurchaseOrder:bc:200",
            "label": "PO 200",
            "state": "late",
            "days_late": 9,
            "verdict": "Late by 9 days.",
            "eta": {"state": "no_confirmation_found", "text": "No confirmation.", "handle": None},
            "steps": [],
            "next_action": {"text": "Ask supplier.", "rule": "no_confirmation"},
            "gaps": [],
        }
        payload = json.dumps({"family": "po_trail", "raw": raw_trail})
        pg = _FakePg([
            ("to_regclass('biz.plan_section_items')", [(False,)]),
            ("FROM biz.plan_section_items", [
                ("PurchaseOrder:bc:200", payload, [], "2026-11-20T08:00:00+00:00"),
            ]),
        ])
        host = type("Host", (), {
            "pg_dsn": "postgresql:///synth",
            "pg": lambda self_: pg,
        })()
        with mock.patch("operator_api.trail.reads_every_item", return_value=True):
            trails = be.WorkspaceBackend._precomputed_po_trails(
                host, _FakeView(), ["PurchaseOrder:bc:200"], {"PurchaseOrder:bc:200": "PO 200"},
            )
        self.assertIn("PurchaseOrder:bc:200", trails)
        self.assertEqual(trails["PurchaseOrder:bc:200"]["built_at"], "2026-11-20T08:00:00+00:00")
        self.assertEqual(trails["PurchaseOrder:bc:200"]["trails"][0]["days_late"], 9)


if __name__ == "__main__":
    unittest.main()
