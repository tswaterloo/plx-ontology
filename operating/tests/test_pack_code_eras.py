"""AUD-13 (audit B #21): a pack code reused over time (``P-0521`` was a Salima pump in the
2023 packaging sheet and is a Biophora pump today) is never one confident match: records
that disagree on their name across dates come back as dated candidates, and a dated
question or document picks the era that covers its date. No Postgres: a stand-in index."""
from __future__ import annotations

import datetime as dt
import unittest

from ontology.types import Actor
from operating import query_understanding as qu
from operating.ontology_tools_policy import ReadPolicy

OLD = 'Product:portal.fm_packaging:{"id":"old"}'
NEW = 'Product:bc.items:{"id":"new"}'
NEW_APP = 'Product:portal.app_product:{"id":"new"}'
OTHER = 'Product:bc.items:{"id":"other"}'


def row(oid, name, valid_from=None, valid_to=None, code="P-0521"):
    return qu.EntityRow(f"e:{oid}", oid, "item", name, qu.fold(name), (code, "P0521"), code,
                        1.0, valid_from=valid_from, valid_to=valid_to)


class Index:
    def __init__(self, rows):
        self.rows = rows

    def scope(self, policy, actor):
        return "scope"

    def by_identifier(self, forms, scope, types=None):
        asked = {f.upper().replace("-", "") for f in forms}
        return (list(self.rows) if "P0521" in asked else []), set()

    def by_terms(self, terms, scope, types=None):
        return [], {}

    def distinctive_words(self, words, scope):
        return set()


ACTOR = Actor("plx", frozenset({"staff"}))
POLICY = ReadPolicy("plx", {"api:*": frozenset({"staff"})}, {})
TODAY = dt.date(2026, 11, 14)


def ask(rows, question="Where is P-0521 used?", **kw):
    result = qu.understand(question, actor=ACTOR, policy=POLICY, index=Index(rows), llm=False,
                           today=TODAY, **kw)
    (mention,) = [m for m in result.entities if m.kind == "identifier" and m.candidates]
    return mention


SALIMA = ("Salima, Pump, 250 ml", "2021-03-01", "2024-03-01")
BIOPHORA = ("Biophora, Pump, Silver, 250ml", "2024-03-01", None)


class Eras(unittest.TestCase):
    def rows(self):
        return [row(OLD, *SALIMA), row(NEW, *BIOPHORA), row(NEW_APP, BIOPHORA[0], BIOPHORA[1])]

    def test_an_undated_question_gets_dated_candidates_never_one_confident_match(self):
        m = ask(self.rows())
        self.assertIsNone(m.resolved)
        self.assertTrue(m.ambiguous)
        self.assertEqual(len(m.choices), 2)        # Salima, and the two copies as one
        self.assertIn(OLD, m.choices)
        self.assertIn("Salima, Pump, 250 ml (2021-03 to 2024-03)", m.prompt)
        self.assertIn("Biophora, Pump, Silver, 250ml (since 2024-03)", m.prompt)
        dated = {c.object_id: (c.valid_from, c.valid_to) for c in m.candidates}
        self.assertEqual(dated[OLD], ("2021-03-01", "2024-03-01"))

    def test_a_dated_document_picks_the_era_that_covers_its_date(self):
        m = ask(self.rows(), as_of="2023-06-15")
        self.assertEqual(m.resolved, OLD)
        self.assertFalse(m.ambiguous)
        self.assertIn("2023-06-15", m.candidates[0].why)
        m = ask(self.rows(), as_of="2025-01-02")
        self.assertIn(m.resolved, {NEW, NEW_APP})   # the two copies are one record

    def test_the_question_s_own_date_picks_the_era(self):
        m = ask(self.rows(), "What was P-0521 on 2023-06-15?")
        self.assertEqual(m.resolved, OLD)

    def test_a_window_spanning_both_eras_or_none_asks(self):
        both = ask(self.rows(), as_of=None, question="P-0521 usage since 2021-01-01 until 2026-01-01")
        self.assertIsNone(both.resolved)
        gap = ask([row(OLD, *SALIMA), row(NEW, "Biophora Pump", "2025-01-01", None)],
                  as_of="2024-06-01")
        self.assertIsNone(gap.resolved)   # no era covers the date: never guess

    def test_a_code_with_no_dated_record_resolves_as_before(self):
        m = ask([row(OLD, SALIMA[0]), row(NEW, BIOPHORA[0])])
        self.assertTrue(m.resolved is not None or m.ambiguous)   # unchanged behaviour
        self.assertFalse(any(c.valid_from for c in m.candidates))

    def test_copies_of_one_record_stay_one_confident_match(self):
        m = ask([row(NEW, *BIOPHORA), row(NEW_APP, *BIOPHORA)])
        self.assertIsNotNone(m.resolved)
        self.assertFalse(m.ambiguous)


if __name__ == "__main__":
    unittest.main()
