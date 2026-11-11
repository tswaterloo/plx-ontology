"""The checkpointed step migration layer (``ontology.step_layer_workers``): built whole it
is ``ontology.step_migrate.build_layer``'s layer, table for table; stopped in a step (one
after another and side by side) and run again, it resumes after its last finished step,
without copying the served rows again or running a finished step again, to the same
layer. A checkpoint of another plan is refused and kept; the copy follows the pinned
``step_migrate.build_layer``."""

from __future__ import annotations

import io
import sqlite3
import unittest
from unittest import mock

from operating.tests.test_pg_step_migrate import StepMigrationTestsDefaultGuard


class StepLayerResumeTests(StepMigrationTestsDefaultGuard):

    def test_a_stopped_step_layer_resumes_to_the_whole_layer(self):
        from ontology import step_layer_workers as slw
        from ontology import step_migrate
        from ontology.step_migrate import plan_steps, steps_digest
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        from tests.test_ontology_value_parts import store_rows

        self.assertEqual(slw.source_digest(), slw.BUILD_LAYER_DIGEST)
        self.fixture.capture(T1)
        base = self.full(1, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        specs = {k: v for k, v in self.specs().items() if v.supported}
        names = (set(self.original(self.fixture.state, "plx")) | {"probe"}) & set(specs)
        steps = plan_steps(self.fixture.schema, self.fixture.schema,
                           {n: "same" for n in specs}, {n: "changed" for n in specs},
                           names, specs)
        plan = {"id": "all-v2", "steps": steps, "steps_from": steps_digest(),
                "steps_to": "steps:all-v2"}
        run = sorted(steps["run"])
        self.assertGreater(len(run), 5)

        def rows_of(out):
            conn = sqlite3.connect(out)
            try:
                rows = store_rows(conn)
                rows["step_migration"] = [r for r in rows["step_migration"]
                                          if r[0] != "built_at"]
                return rows
            finally:
                conn.close()

        real = step_migrate.build_layer
        for width in (1, 4):
            with self.subTest(width=width):
                def whole(*a, _w=width, **k):
                    return real(*a, **{**k, "specs": specs, "steps_side_by_side": _w,
                                       "sequence": 7})
                with mock.patch.object(step_migrate, "build_layer", whole):
                    built, out = self.layer(plan, f"whole-{width}.sqlite")
                expected = (rows_of(out), built["links"], built["id"])

                stop_in = run[len(run) // 2]
                resume_dir = self.fixture.root / f"resume-{width}"
                # the steps run, recorded in a file: a side-by-side step runs in a forked
                # process (step_migrate.run_side_by_side), whose list the test never sees
                ran_file = self.fixture.root / f"ran-{width}.txt"
                ran_file.write_text("")

                def ran_steps():
                    return ran_file.read_text().split()
                copies = [0]
                copy_served = step_migrate.copy_served

                def counted_copy(*a, **k):
                    copies[0] += 1
                    return copy_served(*a, **k)

                def checkpointed(*a, _w=width, stop=None, identity=None, **k):
                    held = slw.Checkpoints(resume_dir, identity or {"plan": "all-v2"})
                    steps_ = dict(a[2])
                    for name in list(steps_):
                        def step(builder, projector, _n=name, _f=steps_[name]):
                            with open(ran_file, "a") as f:
                                f.write(_n + "\n")
                            if _n == stop:
                                raise KeyboardInterrupt(f"stopped in {_n}")
                            return _f(builder, projector)
                        steps_[name] = step
                    try:
                        return slw.build_layer(a[0], a[1], steps_, *a[3:],
                                               **{**k, "specs": specs,
                                                  "steps_side_by_side": _w, "sequence": 7,
                                                  "checkpoints": held})
                    finally:
                        held.close()

                with mock.patch.object(step_migrate, "copy_served", counted_copy):
                    with mock.patch.object(step_migrate, "build_layer",
                                           lambda *a, **k: checkpointed(*a, stop=stop_in,
                                                                        **k)), \
                            self.assertRaises(KeyboardInterrupt):
                        self.layer(plan, f"resumed-{width}.sqlite")
                    self.assertEqual(copies[0], 1)
                    before = ran_steps()
                    with mock.patch.object(step_migrate, "build_layer",
                                           lambda *a, **k: checkpointed(
                                               *a, identity={"plan": "other"}, **k)), \
                            self.assertRaises(slw.ResumeRefused):
                        self.layer(plan, f"resumed-{width}.sqlite")
                    ran_file.write_text("")
                    with mock.patch.object(step_migrate, "build_layer", checkpointed):
                        built, out = self.layer(plan, f"resumed-{width}.sqlite")
                self.assertEqual(copies[0], 1)              # the input was not copied again
                self.assertIn("resumed", built)
                ran = ran_steps()
                finished = [n for n in before if n != stop_in and before.index(n) <
                            before.index(stop_in)] if width == 1 else []
                self.assertTrue(set(finished).isdisjoint(ran), (finished, ran))
                self.assertIn(stop_in, ran)
                self.assertLess(len(ran), len(run))           # not every step again
                self.assertEqual((rows_of(out), built["links"], built["id"]), expected)


if __name__ == "__main__":
    unittest.main()
