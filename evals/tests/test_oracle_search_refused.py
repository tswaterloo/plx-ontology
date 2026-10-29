"""Refused document searches are no sample of what names the roots (directives 419724c6c569
items 1 and 3, d0125cbdcdf6; gap-04f9b49ebae9). Fixture world and fake readers; injected,
not live.

The live acceptance alternative to a candidate is ``no_document_names_root`` with
``funnel.roots_searched >= 20``: refused searches must neither count as roots searched nor
let a mostly refused stratum pass as a sample (judged per root: more roots refused than
not), a reader's own ValueError is a failed search, never a guard refusal, and the read-only
guard's refusal is one.
"""

from __future__ import annotations

import unittest

from evals.oracle import PortalReads, Sources
from evals.oracle.fixture_world import World, build_live_shaped_world
from evals.oracle.generator import (MAX_ROOT_SEARCHES, PORTAL_STRATA, document_candidates,
                                    fallback_query, portal_candidates, root_queries,
                                    search_roots, search_status)
from integrations import live_source_grammar as grammar
from integrations.live_source_guard import NotVisible, ReadOnlyGuardError
from integrations.tests.test_bc_purchase import BUYER
from scripts import eval_oracle_dev as dev

RUN = "live-shaped-a"


class SearchRefusedTest(unittest.TestCase):
    def setUp(self):
        world = build_live_shaped_world()
        self.sources = Sources(PortalReads(world.read_relation), None)
        self.pool = [c for s in PORTAL_STRATA for c in portal_candidates(self.sources, s)]
        self.roots = search_roots(self.pool, RUN)[:MAX_ROOT_SEARCHES]
        self.assertEqual(len(self.roots), MAX_ROOT_SEARCHES)

    def search(self, port):
        funnel, read = {}, {}
        found = document_candidates(self.sources, port, "mail", self.pool, funnel=funnel,
                                    read=read, run_id=RUN)
        return found, funnel, read

    def test_refused_searches_are_not_roots_searched_and_mostly_refused_is_typed(self):
        empty = World({}, documents={"mail": []}).documents()   # answers, names nothing
        for answered, status in ((4, ("unavailable", "search_refused")),
                                 (20, ("no_candidate_in_sample", "no_document_names_root"))):
            allowed = {q for root in self.roots[:answered]
                       for q in root_queries(self.sources, root)}
            allowed |= {fallback_query(q) for q in allowed} - {None}

            class Port:
                def search(self, kind, queries, *, hits, seed=""):
                    answer = empty.search(kind, [q for q in queries if q in allowed], hits=hits)
                    answer["results"] += [{"query": q, "refused": "guard"}
                                          for q in queries if q not in allowed]
                    return answer

            found, funnel, read = self.search(Port())
            self.assertEqual(found, [])
            self.assertEqual((funnel["roots"] >= MAX_ROOT_SEARCHES, funnel["roots_searched"]),
                             (True, answered))
            self.assertGreater(funnel["search_refused_guard"], 0)
            self.assertEqual(search_status(read, funnel), status, funnel)

    def test_a_reader_value_error_is_a_failed_search_and_a_guard_refusal_is_typed(self):
        reached: list[str] = []

        class Drive:
            def search(self, actor, query, *, max_items):
                reached.append(query)
                if len(reached) == 1:
                    raise ValueError("the reader's own bug, not a refusal")
                if len(reached) == 2:
                    raise NotVisible()
                return {"items": [], "completeness": {"truncated": False}}

        docs = dev.LiveDocuments(BUYER, drive_reader=Drive(), drive_paths=["/Documents"])
        sent: list[str] = []
        search = docs.search

        def recorded(kind, queries, **kwargs):
            sent.extend(queries)
            return search(kind, queries, **kwargs)

        docs.search = recorded
        funnel, read = {}, {}
        document_candidates(self.sources, docs, "sharepoint", self.pool, funnel=funnel,
                            read=read, run_id=RUN)
        outside = [q for q in sent if not grammar.search_text_ok(q)]
        self.assertGreater(len(reached), 2)
        self.assertEqual(len(reached) + len(outside), len(sent))   # outside: never sent
        self.assertEqual(funnel["search_failed"], 1)
        self.assertEqual(funnel.get("search_refused_guard", 0), len(outside) + 1)
        self.assertEqual(funnel["search_refused"], funnel["search_refused_guard"])
        self.assertFalse(read["complete"])
        self.assertEqual(search_status(read, funnel), ("unreadable", "search_failed"))

    def test_mostly_refused_is_judged_per_root_not_per_query(self):
        # A refused root asks its number and, as nothing named it, the number without its
        # letters: two refused queries per root. Half the roots refused is most queries
        # refused but not most roots; one root more is, also when a refusal names no query
        # (it may be any unanswered root's: each counts refused, never searched).
        twice = [r for r in self.roots
                 if any(fallback_query(q) for q in root_queries(self.sources, r))]
        half = len(self.roots) // 2
        self.assertGreater(len(twice), half)
        empty = World({}, documents={"mail": []}).documents()
        for refused_roots, named, status in (
                (half, True, ("no_candidate_in_sample", "no_document_names_root")),
                (half + 1, True, ("unavailable", "search_refused")),
                (half + 1, False, ("unavailable", "search_refused"))):
            refused = {q for root in twice[:refused_roots]
                       for q in root_queries(self.sources, root)}
            refused |= {fallback_query(q) for q in refused} - {None}

            class Port:
                def search(self, kind, queries, *, hits, seed=""):
                    answer = empty.search(kind, [q for q in queries if q not in refused],
                                          hits=hits)
                    answer["results"] += [{"query": q, "refused": "tenant"} if named
                                          else {"refused": "tenant"}
                                          for q in queries if q in refused]
                    return answer

            found, funnel, read = self.search(Port())
            self.assertEqual(found, [])
            self.assertEqual((funnel["roots_asked"], funnel["roots_refused"],
                              funnel["roots_searched"]),
                             (len(self.roots), refused_roots, len(self.roots) - refused_roots))
            asked = funnel["searches"] + funnel["fallback_searches"]
            self.assertGreater(2 * funnel["search_refused"], asked)   # most queries refused
            self.assertEqual(search_status(read, funnel), status, funnel)

    def test_a_read_only_guard_refusal_is_typed_and_a_find_beside_it_is_incomplete(self):
        # The read-only guard refuses the reader's search before any I/O: a guard refusal,
        # never a failed search. A document found by the few searches answered makes the
        # mostly refused stratum incomplete, not unavailable.
        answered = {q for root in self.roots[:4] for q in root_queries(self.sources, root)}
        answered |= {fallback_query(q) for q in answered} - {None}
        reached: list[str] = []

        class Drive:
            def search(self, actor, query, *, max_items):
                reached.append(query)
                if query not in answered:
                    raise ReadOnlyGuardError("refused before any I/O")
                return {"items": [{"citation": "sharepoint://fx-drive/menu@e1", "id": "menu",
                                   "kind": "file", "name": "Lunch menu.png"}],
                        "completeness": {"truncated": False}}

        docs = dev.LiveDocuments(BUYER, drive_reader=Drive(), drive_paths=["/Documents"])
        funnel, read = {}, {}
        found = document_candidates(self.sources, docs, "sharepoint", self.pool,
                                    funnel=funnel, read=read, run_id=RUN)
        self.assertEqual(found, [])
        refused = [q for q in reached if q not in answered]
        self.assertTrue(refused)
        self.assertEqual(funnel["search_refused_guard"], len(refused))
        self.assertNotIn("search_failed", funnel)
        self.assertEqual((funnel["roots_searched"], funnel["roots_refused"],
                          funnel["documents"]), (4, len(self.roots) - 4, 1))
        self.assertFalse(read["complete"])
        self.assertEqual(search_status(read, funnel), ("incomplete", "search_refused"))


if __name__ == "__main__":
    unittest.main()
