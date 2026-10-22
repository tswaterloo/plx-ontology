"""The cross-system sample's documents are searched one way: ``generator.document_candidates``.

Gap-f713876847b6: the unfamiliar sample listed mail and
SharePoint on its own path (capped listings in time windows, its own token index) while the
dev set's document strata searched root first; it now takes its document references through
``generator.document_candidates`` and says why a kind has none the way the dev set does
(``generator.search_status``): unreadable is not empty, a failed root
lookup leaves no clean sample, and a budget stop is ``blocked: budget``. Injected ports over
the live-shaped fixture world; not live.
"""

from __future__ import annotations

import unittest

from evals.oracle import BcReads, PortalReads, Sources
from evals.oracle import cross_sample as cs
from evals.oracle.fixture_world import build_live_shaped_world
from evals.oracle.generator import BC_INVOICE_SETS, document_candidates
from evals.oracle.source import BUDGET_STOP

RUN = "live-shaped-a"


class Searched:
    """The fixture world's document search, with no ``sample``: the one path. ``answer``
    rewrites each search's answer (a withheld hit, a failed query)."""

    def __init__(self, world, answer=lambda found: found) -> None:
        self.inner, self.answer, self.asked = world.documents(), answer, []
        self.max_roots = self.inner.max_roots

    def search(self, kind, queries, **kwargs):
        self.asked.append(kind)
        return self.answer(self.inner.search(kind, queries, **kwargs))


class OnePathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = build_live_shaped_world()

    def sources(self) -> Sources:
        return Sources(PortalReads(self.world.read_relation), BcReads(self.world.bc_port()))

    def test_document_references_are_the_roots_document_candidates_names(self):
        sources = self.sources()
        port = Searched(self.world)
        drawn = cs.sample(sources, run_id=RUN, documents=port)
        roots = [r for kind in cs.RECORD_KINDS if kind != "bc_invoice"
                 for r in cs.portal_roots(sources, kind)] + cs.bc_roots(sources, 50)
        texts = {kind: [t for d in self.world.docs[kind] for t in d["texts"]]
                 for kind in cs.DOCUMENT_KINDS}
        for kind in cs.DOCUMENT_KINDS:
            named = {(c.root_relation, c.root_key) for c in document_candidates(
                sources, port, kind, [cs._candidate(r) for r in roots], port.max_roots,
                run_id=RUN)}
            items = [i for i in drawn.items if i.kind == kind]
            self.assertTrue(items, kind)
            self.assertEqual(drawn.kinds[kind]["references"], len(named), kind)
            for item in items:   # a root it names, asked as a document wrote it
                self.assertIn((item.relation, item.key), named)
                self.assertTrue(any(item.identifier in t for t in texts[kind]), item)
        self.assertEqual(set(port.asked), set(cs.DOCUMENT_KINDS))

    def test_a_kind_without_references_says_why_never_empty(self):
        def withheld(found):
            return {**found, "records": [], "results": [
                {**r, "handles": [], "withheld": 1} for r in found["results"]]}

        def failed(found):
            return {**found, "records": [], "results": [
                {"query": r["query"], "failed": "unavailable"} for r in found["results"]]}

        for answer, status, reason in ((withheld, "unreadable", "withheld"),
                                       (failed, "unreadable", "search_failed")):
            kinds = cs.sample(self.sources(), run_id=RUN,
                              documents=Searched(self.world, answer)).kinds
            for kind in cs.DOCUMENT_KINDS:
                self.assertEqual((kinds[kind]["status"], kinds[kind]["reason"]),
                                 (status, reason), (kind, kinds[kind]))
        # BC fails every lookup of the invoice numbers the documents name: no clean sample
        numbers = [r["fields"]["number"] for s in BC_INVOICE_SETS for r in self.world.bc[s]]
        for kind in cs.DOCUMENT_KINDS:
            self.world.docs[kind] = [{"handle": f"fixture://{kind}/bc-{i}",
                                      "texts": [f"Invoice {n} attached"]}
                                     for i, n in enumerate(numbers)]
        kinds = cs.sample(self.sources(), run_id=RUN, documents=Searched(self.world)).kinds
        for kind in cs.DOCUMENT_KINDS:   # the documents do name roots
            self.assertEqual(kinds[kind]["references"], len(numbers), kind)
        self.world.fail.update(BC_INVOICE_SETS)
        kinds = cs.sample(self.sources(), run_id=RUN, documents=Searched(self.world)).kinds
        for kind in cs.DOCUMENT_KINDS:
            entry = kinds[kind]
            self.assertEqual((entry["status"], entry["reason"], entry["references"],
                              entry["skipped"]),
                             ("incomplete", "root_lookup_failed", 0,
                              {"reference_read_gap": len(numbers)}), kind)

    def test_a_budget_stop_blocks_the_kind_on_the_budget(self):
        stop = type(BUDGET_STOP, (RuntimeError,), {})

        def stopped(found):
            raise stop("sampling budget spent")

        kinds = cs.sample(self.sources(), run_id=RUN,
                          documents=Searched(self.world, stopped)).kinds
        for kind in cs.DOCUMENT_KINDS:
            self.assertEqual((kinds[kind]["status"], kinds[kind]["blocked"]),
                             ("unavailable", "budget"), kind)


if __name__ == "__main__":
    unittest.main()
