"""A kept page is keyed by the owner mark and by the people-exclusion set as it stands.

The owner sees the partners' rows and nobody else does, and the set of what the exclusion
withholds grows with each update. A page (or a search scope) kept before either changed
must not be found again after: its counts and aggregates may have been built over rows
the reader may no longer count."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ontology.types import Actor
from operating.ontology_tools_policy import ReadPolicy
from operating.pg import people_excluded, permissions as perm
from operator_api.backend import page_key
from operator_api.serving import ReaderCache

OID = "22222222-2222-4222-8222-222222222222"
ACTOR = Actor(tenant="plx", principals=frozenset({f"employee:{OID}"}))


def policy(owner: bool) -> ReadPolicy:
    return ReadPolicy(tenant="plx", audiences={"mail:*": frozenset(ACTOR.principals)},
                      markings={}, unrestricted=owner, owner=owner)


def view(pol: ReadPolicy) -> SimpleNamespace:
    access = SimpleNamespace(reader_policy=lambda: (ACTOR, pol),
                             store_meta=lambda: {"content_digest": "gen-1"},
                             delta_layer_stamp=lambda: ("layers", 1))
    return SimpleNamespace(access=access)


class PageKeyExclusionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.set = people_excluded.Excluded("ids:tag", 100)
        patch = mock.patch.object(people_excluded, "excluded", lambda: self.set)
        patch.start()
        self.addCleanup(patch.stop)
        marks = mock.patch("operator_api.backend._marks_stamp", lambda v: None)
        marks.start()
        self.addCleanup(marks.stop)

    def test_kept_page_is_not_served_after_the_set_grows(self) -> None:
        cache = ReaderCache()
        reader = view(policy(owner=False))
        before = page_key(reader, "Customer:x")
        self.assertIsNotNone(before)
        cache.put(before, {"hidden": 3})   # counts built over the set's rows
        self.assertEqual(page_key(reader, "Customer:x"), before)
        self.set = people_excluded.Excluded("ids:tag", 101)   # the update decided more
        after = page_key(reader, "Customer:x")
        self.assertNotEqual(after, before)
        self.assertIsNone(cache.get(after))
        self.set = people_excluded.Excluded("ids:other", 101)   # another list
        self.assertNotEqual(page_key(reader, "Customer:x"), after)
        self.set = None   # no set in use: a key of its own, never the set's
        self.assertNotIn(page_key(reader, "Customer:x"), (before, after))

    def test_owner_mark_is_part_of_the_key(self) -> None:
        owner, other = view(policy(owner=True)), view(policy(owner=False))
        self.assertNotEqual(page_key(owner, "Customer:x"), page_key(other, "Customer:x"))
        self.assertNotEqual(perm._fingerprint_of(policy(True), ACTOR),
                            perm._fingerprint_of(policy(False), ACTOR))

    def test_unreadable_set_never_matches_a_read_one(self) -> None:
        def broken() -> None:
            raise RuntimeError("down")
        stamps = {repr(perm.exclusion_set_stamp())}
        with mock.patch.object(people_excluded, "excluded", broken):
            stamps.add(repr(perm.exclusion_set_stamp()))
        self.assertEqual(len(stamps), 2)


if __name__ == "__main__":
    unittest.main()
