"""session.replay over the warm store (roadmap A7/B2/B5).

Every new verifier session used to run both session producers (``mirror.full_coverage``,
``ontology.session_projection``: some 3,700 s live) before a consumer, even when the warm
store (``operating.warm_store``) held the mirror and projection the last session filled.
Now a consumer adopts them (the live verifier.SessionState.plan``'s ``warm``, the one
rule for every session consumer): its producers are not run, and it opens the earlier
session's generation, which is the store a cold build of the same captures makes.

Evidence label: ``injected-transport``. The specs' own argv over the fixture Portal, the BC
fake and the Graph fake through two ``SessionState`` sessions over one
``PLX_WORKBENCH_STATE_DIR``; nothing live (the spec stays live-unverified)."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from devloop import live_verify as lv
from devloop.tests import test_live_wiring as wiring
from operating.warm_store import STATE_ENV, WarmStore
from scripts import object_workspace_probe as workspace
from scripts import ontology_projection_probe as projection
from tests import test_object_workspace_http as http

SPECS = lv.valid_specs(lv.load_specs(lv.ROOT / lv.SPECS_DIR_NAME))
MIRROR, PRODUCER = SPECS["mirror.full_coverage"], SPECS["ontology.session_projection"]
REPLAY = SPECS["session.replay"]
PRODUCERS = ("mirror.full_coverage", "ontology.session_projection")
LABELS = {"bc_capture.evidence_label != expected", "graph_mail_capture.evidence_label != expected"}


class World:
    """The fixture world and one ``PLX_WORKBENCH_STATE_DIR`` that verifier sessions share."""

    def __init__(self, stack: ExitStack) -> None:
        self.tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(wiring.fixture_bound())
        env = stack.enter_context(http.ObjectViewLiveCheckTest.fixture_sources(REPLAY))
        self.environ = {**env, STATE_ENV: f"{self.tmp}/state"}
        os.makedirs(self.environ[STATE_ENV])
        self.runs = 0

    def session(self, clock=None) -> lv.SessionState:
        warm, detail = WarmStore.open(self.environ, **({"clock": clock} if clock else {}))
        assert warm is not None, detail
        return lv.SessionState(lv.ROOT, warm=warm, environ=self.environ)

    def sync(self, session: lv.SessionState) -> None:
        """``mirror.full_coverage``: the session mirror synced from the fixture Portal."""
        from integrations import workbench_read_api
        from scripts.mirror_measure import ordered_full_coverage

        plan = session.plan(MIRROR, SPECS)
        ordered_full_coverage(workbench_read_api.WorkbenchReadApiClient(
            wiring.FIXTURE_URL, wiring.PORTAL_ENV["PLX_WORKBENCH_API_KEY"]),
            session.bases(MIRROR, plan)["/mirror.sqlite"] + "/mirror.sqlite", resolve_sample=0)
        session.ran.add("mirror.full_coverage")
        session.record(MIRROR, plan, {"id": "mirror.full_coverage", "result": "pass",
                                      "exit_code": 0, "detail": {"fields": {}}})

    def run(self, session: lv.SessionState, spec_doc, main, bases=None, since=None):
        """``spec_doc`` by its own argv in ``session`` (``bases``: expand there instead,
        unrecorded), with the freshness start the verifier passes (``since``: this one);
        (plan, code, report)."""
        plan = session.plan(spec_doc, SPECS)
        self.runs += 1
        held = session.bases(spec_doc, plan) if bases is None else bases
        argv = lv._expand(spec_doc["command"][3:], f"{self.tmp}/run-{self.runs}", None, held)
        environ = {**self.environ, lv.CAPTURE_FRESH_SINCE_ENV:
                   since or session.since(spec_doc, plan)}
        fake, books = wiring.fixture_bc()
        code, document = wiring.run_main(main, argv, environ=environ, bc_sender=fake,
                                         bc_books=books)
        if bases is None:
            session.ran.add(spec_doc["id"])
            session.record(spec_doc, plan, {
                "id": spec_doc["id"], "result": "pass" if code == 0 else "fail",
                "exit_code": code, "detail": {"fields": document}})
        return plan, code, document

    def first(self) -> None:
        """A session that runs both producers, then session.replay sharing them."""
        session = self.session()
        try:
            self.sync(session)
            _, code, _ = self.run(session, PRODUCER, projection.main)
            assert code == 0
            plan, code, document = self.run(session, REPLAY, workspace.main)
            assert (plan["/mirror.sqlite"], plan["/projection"]) == ("shared", "shared"), plan
            assert code == 0, document
        finally:
            session.close()


def cold_build(root: Path, mirror: str, bc: str) -> str:
    """The content digest of a cold build of ``mirror`` and the BC capture ``bc`` in
    ``root``: the session producer's own ``build_generation`` call."""
    from ontology.project import MirrorCaptures, _default_normalizer
    from ontology.project_bc import BcCaptures
    from ontology.project_incremental import build_generation
    from ontology.schema import load_schema
    from operating import build_steps

    with MirrorCaptures(mirror) as portal, BcCaptures(bc) as books:
        generation = build_generation(root, load_schema(), {"portal_api": portal, "bc": books},
                                      normalizer=_default_normalizer(),
                                      post_project=build_steps.post_project(root,
                                                                            projection.TENANT))
    assert generation.mode == "cold", generation.mode
    return generation.content_digest


def needed(session: lv.SessionState, spec_doc) -> set[str]:
    """The session producers the daily rotation runs first for ``spec_doc``."""
    return {up for ups in session.needs_producers(spec_doc, SPECS).values() for up in ups
            if up not in session.ran}


class WarmAdoptionTest(unittest.TestCase):

    def setUp(self) -> None:
        self.cwd = os.getcwd()
        os.chdir(lv.ROOT)
        self.addCleanup(os.chdir, self.cwd)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.world = World(self.stack)

    def test_a_new_session_replays_over_the_kept_stores_without_running_its_producers(self):
        world = self.world
        world.first()
        session = world.session()
        try:
            plan = session.plan(REPLAY, SPECS)
            self.assertEqual((plan["/mirror.sqlite"], plan["/projection"]), ("warm", "warm"))
            self.assertEqual(needed(session, REPLAY), set())   # neither producer runs
            with mock.patch.object(projection, "_produce",
                                   side_effect=AssertionError("producer ran")):
                plan, code, document = world.run(session, REPLAY, workspace.main)
            self.assertEqual(code, 0, document)
            self.assertEqual(set(lv.check_expectations(REPLAY, code, document)) - LABELS,
                             set(), document)
            replayed = document["session_replay"]
            self.assertEqual((replayed["sessions_recorded"], replayed["replay_identical"],
                              replayed["withheld_reader_markers"]), (3, 3, 3))
            self.assertNotEqual(document["session_projection"]["mode"], "cold", document)
            detail = session.detail(REPLAY, plan, SPECS)
            self.assertTrue(detail["shared_store"])
            self.assertEqual(set(detail["producer_verdict"].values()), {"pass"})
            # The adopted generation is the store a cold build of the same captures makes
            # (the producer's own build call, into a directory of its own).
            bases = session.bases(REPLAY, dict(plan))
            since = session.since(REPLAY, plan)
            self.assertLess(since, session.started_at)   # the adopted session's start
            opened = projection.open_session(bases["/projection"] + "/projection", since)
            self.assertIsNotNone(opened)
            self.assertEqual(opened["mirror"],
                             str(Path(bases["/mirror.sqlite"], "mirror.sqlite").resolve()))
            self.assertIsNone(projection.open_session(bases["/projection"] + "/projection",
                                                      session.started_at))
            self.assertEqual(cold_build(Path(world.tmp, "cold"), opened["mirror"],
                                        opened["bc"]),
                             opened["generation"].content_digest)
            # A second consumer of the same session adopts the same stores.
            self.assertEqual(session.plan(SPECS["workspace.object_view"], SPECS)["/projection"],
                             "warm")
        finally:
            session.close()
        # What was adopted is kept again for the next session, still adoptable.
        session = world.session()
        try:
            self.assertEqual(needed(session, REPLAY), set())
        finally:
            session.close()

    def test_a_projection_is_never_adopted_without_the_mirror_it_was_built_from(self):
        world = self.world
        world.first()
        only = [SPECS[sid] for sid in ("ontology.money_units", "eval.unfamiliar_sample")]
        session = world.session()
        try:   # these read the projection alone: its mirror would not come along
            for spec_doc in only:
                self.assertEqual(set(spec_doc.get("session_reads", ())), {"/projection"})
                self.assertEqual(session.plan(spec_doc, SPECS)["/projection"], "cold_not_run")
                self.assertEqual(needed(session, spec_doc), {"ontology.session_projection"})
            # Once a consumer adopted both, the projection opens with its mirror beside it.
            plan = session.plan(REPLAY, SPECS)
            session.bases(REPLAY, plan)
            self.assertEqual(plan["/projection"], "warm")
            for spec_doc in only:
                held = session.plan(spec_doc, SPECS)
                self.assertEqual(held["/projection"], "warm")
                self.assertEqual(needed(session, spec_doc), set())
                bases = session.bases(spec_doc, held)
                self.assertIsNotNone(projection.open_session(
                    bases["/projection"] + "/projection", session.since(spec_doc, held)))
        finally:
            session.close()

    def test_a_store_is_not_adopted_across_sessions_or_after_its_input_changed(self):
        world = self.world
        world.first()
        # The mirror synced again in a later session: the kept projection was built from
        # other rows, so a consumer that shares this mirror needs the session producer.
        session = world.session()
        try:
            world.sync(session)
            plan = session.plan(REPLAY, SPECS)
            self.assertEqual((plan["/mirror.sqlite"], plan["/projection"]),
                             ("shared", "cold_not_run"))
            self.assertEqual(needed(session, REPLAY), {"ontology.session_projection"})
        finally:
            session.close()
        # Kept now: the mirror of that session, the projection of the first: never paired.
        session = world.session()
        try:
            plan = session.plan(REPLAY, SPECS)
            self.assertNotIn("warm", plan.values())
            self.assertEqual(needed(session, REPLAY), set(PRODUCERS))
        finally:
            session.close()

    def test_a_mirror_rewritten_after_adoption_spoils_the_adopted_projection(self):
        world = self.world
        world.first()
        session = world.session()
        try:
            plan = session.plan(REPLAY, SPECS)
            session.bases(REPLAY, plan)
            self.assertEqual(plan["/projection"], "warm")
            world.sync(session)   # the adopted mirror is written in this session
            plan = session.plan(REPLAY, SPECS)
            self.assertEqual((plan["/mirror.sqlite"], plan["/projection"]),
                             ("shared", "cold_not_run"))
        finally:
            session.close()
        # The carried projection is kept incoherent: never adopted again.
        session = world.session()
        try:
            self.assertNotIn("warm", session.plan(REPLAY, SPECS).values())
        finally:
            session.close()

    def test_a_projection_its_session_outdated_or_one_too_old_is_not_adopted(self):
        world = self.world
        session = world.session()
        try:   # the mirror synced again after the projection was built from it
            world.sync(session)
            self.assertEqual(world.run(session, PRODUCER, projection.main)[1], 0)
            world.sync(session)
        finally:
            session.close()
        session = world.session()
        try:
            self.assertNotIn("warm", session.plan(REPLAY, SPECS).values())
            self.assertEqual(needed(session, REPLAY), set(PRODUCERS))
        finally:
            session.close()
        world.first()
        later = datetime.now(timezone.utc) + timedelta(hours=27)
        session = world.session(clock=lambda: later)
        try:
            self.assertNotIn("warm", session.plan(REPLAY, SPECS).values())
        finally:
            session.close()

    def test_another_audience_or_no_warm_store_adopts_nothing(self):
        world = self.world
        world.first()
        other = {**world.environ, "PLX_WORKBENCH_API_KEY": "another-key"}
        warm, _ = WarmStore.open(other)
        session = lv.SessionState(lv.ROOT, warm=warm, environ=other)
        try:
            self.assertNotIn("warm", session.plan(REPLAY, SPECS).values())
            self.assertEqual(needed(session, REPLAY), set(PRODUCERS))
        finally:
            session.close()
        session = lv.SessionState(lv.ROOT, warm=None, environ=world.environ)
        try:
            self.assertNotIn("warm", session.plan(REPLAY, SPECS).values())
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
