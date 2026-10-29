"""The projection probe tops up a capture it shares (the verifier's ``{session_state}``).

Live, every check that projected BC captured it cold inside its own budget and none
finished (the live verifier, "Session state"). A probe pointed at a store a session
producer filled must carry that capture forward: resume what is paused, read what is
missing and never read again what this session completed (a store from an earlier
invocation is read again: integrations/tests/test_bc_read_topup.py). Injected transport
(the BC fake of ``integrations/tests/test_bc_capture.py``); the clock is the fake's
request count, so the budget cut is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest

from integrations import bc_read
from integrations.tests import test_bc_capture as tb
from scripts import ontology_projection_probe as probe


def run(tmp: str, fake: tb.FakeBc, budget: float | None,
        environ: dict = tb.ENV) -> tuple[dict, list]:
    before = len(fake.calls)
    result = probe.projection_probe(
        None, f"{tmp}/mirror.sqlite", f"{tmp}/store.sqlite", budget_seconds=budget,
        claim_sample=10, principal="staff:reader",
        bc={"environ": environ, "sender": fake, "books": tb.BOOKS},
        monotonic=lambda: float(len(fake.calls)))
    return result, [url.split("?")[0] for method, url, _ in fake.calls[before:]
                    if method == "GET"]


class StoreReuseTest(unittest.TestCase):
    def test_the_probe_tops_up_an_existing_capture_and_reads_nothing_twice(self) -> None:
        with tempfile.TemporaryDirectory() as cold:
            whole, cold_reads = run(cold, tb.FakeBc(), None)
        self.assertIs(whole["bc_capture"]["topped_up"], False)
        # One verifier session: its start is every check's FRESH_SINCE_ENV.
        session = {**tb.ENV, bc_read.FRESH_SINCE_ENV:
                   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        with tempfile.TemporaryDirectory() as shared:
            fake = tb.FakeBc()
            first, first_reads = run(shared, fake, 150, session)  # the producer: cut by budget
            second, second_reads = run(shared, fake, None, session)  # a consumer of its store
            third, third_reads = run(shared, fake, None, session)
        self.assertEqual(first["bc_capture"]["stopped_reason"], "budget")
        self.assertLess(first["bc_capture"]["units_complete"],
                        whole["bc_capture"]["units_complete"])
        self.assertLess(first["bc_capture"]["units_reached"], first["bc_capture"]["units_total"])
        self.assertEqual([r["bc_capture"]["topped_up"] for r in (first, second, third)],
                         [False, True, True])
        # The top-up finishes the capture without reading a unit the producer finished:
        # the two runs read what one cold capture reads, plus the company listing again.
        listing = {url for url in cold_reads if url.endswith("/companies")}
        self.assertEqual(set(first_reads) & set(second_reads), listing)
        self.assertEqual(len(first_reads) + len(second_reads), len(cold_reads) + len(listing))
        for key in ("units_total", "units_reached", "units_complete", "records_current"):
            self.assertEqual(second["bc_capture"][key], whole["bc_capture"][key], key)
        self.assertEqual((second["objects"], second["values"]),
                         (whole["objects"], whole["values"]))
        # Once complete, a later consumer reads no unit again and projects the same.
        self.assertEqual(set(third_reads), listing)
        self.assertEqual((third["objects"], third["values"]),
                         (whole["objects"], whole["values"]))


if __name__ == "__main__":
    unittest.main()
