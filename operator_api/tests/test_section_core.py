"""A record's sections that need only its id and type start from its card (the core read) and
no longer wait for the page's walk; the others keep the quick read; a record the reader cannot
see is not found with no section read; a card that cannot be read falls back to the quick read.
isolated-fixture; no network."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from operator_api import backend as be
from operator_api import page_core
from operator_api.errors import RecordNotFound


class View:
    @contextlib.contextmanager
    def _request(self):
        yield


class Section(unittest.TestCase):
    def run_section(self, section, *, core=None):
        calls = []
        page = object()
        quick = object()

        def core_page(backend, view, otype, oid):
            calls.append("core")
            if isinstance(core, Exception):
                raise core
            return page

        def quick_page(backend, view, oid):
            calls.append("quick")
            return quick

        def kept_section(backend, view, p, name):
            calls.append(("section", "core" if p is page else "quick", name))
            return "out"

        with mock.patch.object(be, "page_oid", lambda b, v, t, i: "oid"), \
                mock.patch.object(be, "quick_page", quick_page), \
                mock.patch.object(be, "kept_section", kept_section), \
                mock.patch.object(page_core, "core_page", core_page):
            try:
                out = be.object_section(object(), View(), "purchase_order", "PO1", section)
            except RecordNotFound:
                out = "not found"
        return out, calls

    def test_the_sections_that_need_only_the_record_start_from_its_card(self):
        for section in ("late", "people", "landed_cost", "knows", "documents", "formula"):
            out, calls = self.run_section(section)
            self.assertEqual(out, "out")
            self.assertEqual(calls, ["core", ("section", "core", section)], section)

    def test_the_trail_and_the_margin_keep_the_quick_read(self):
        for section in ("trail", "margin"):
            out, calls = self.run_section(section)
            self.assertEqual(calls, ["quick", ("section", "quick", section)], section)

    def test_a_record_the_reader_cannot_see_is_not_found_and_no_section_is_read(self):
        out, calls = self.run_section("late", core=RecordNotFound())
        self.assertEqual(out, "not found")
        self.assertEqual(calls, ["core"])

    def test_a_card_that_cannot_be_read_falls_back_to_the_quick_read(self):
        out, calls = self.run_section("people", core=page_core.CoreUnread("oid"))
        self.assertEqual(out, "out")
        self.assertEqual(calls, ["core", "quick", ("section", "quick", "people")])


if __name__ == "__main__":
    unittest.main()
