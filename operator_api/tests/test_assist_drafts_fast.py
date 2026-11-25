"""Tests for fast GET /api/autopilot/drafts: _serving caching, prewarm registration,
invalidation on writes, pre-filtered fail-closed door checks, and zero redundant SQLite opens."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from operating import autopilot as ap
from operating.card_actions import Journal
from operator_api import assist_routes
from operator_api import models as m
from operator_api.backend_common import PREWARM_SCREENS, SCREENS_AHEAD
from operator_api.tests import test_autopilot_area as base
from operator_api.tests.test_last_good_page_screens import OtherScreens, reader


NOW = datetime.now(timezone.utc) - timedelta(seconds=5)


class AssistDraftsCachingTests(OtherScreens):
    def test_assist_drafts_is_prewarmed_and_read_ahead(self) -> None:
        self.assertIn("assist_drafts", SCREENS_AHEAD)
        self.assertIn(("assist_drafts", False), PREWARM_SCREENS)

    def test_assist_drafts_serves_kept_snapshot_and_refreshes_on_bump(self) -> None:
        who = reader("grp-a")
        other = reader("grp-b")
        calls: list[tuple[str, bool]] = []

        def fake_matters_list(_backend, r, *, all_ages=False, **_kw):
            calls.append((r.key, all_ages))
            return m.AutopilotDrafts(
                items=[],
                never_senders=[f"{r.key}:{len(calls)}"],
                status="ok",
            )

        with patch.object(assist_routes, "matters_list", fake_matters_list):
            first = self.backend.assist_drafts(who)
            second = self.backend.assist_drafts(who)
            self.assertEqual(second.never_senders, first.never_senders)
            self.assertEqual(calls, [(who.key, False)])

            # Different reader never gets who's cached snapshot.
            self.backend.assist_drafts(other)
            self.assertEqual(calls, [(who.key, False), (other.key, False)])

            # Separate all_ages=True cache slot.
            self.backend.assist_drafts(who, all_ages=True)
            self.assertEqual(calls[-1], (who.key, True))

            # Bumping after a matter mutation invalidates the kept copy and triggers refresh.
            assist_routes._bump_assist_drafts(self.backend)
            stale = self.backend.assist_drafts(who)
            self.assertIsNotNone(stale.stale_since)
            self.background_reads_over()
            self.assertEqual(sum(1 for k, a in calls if k == who.key and not a), 2)


class AssistDoorAndColdPathTests(base.AutopilotArea):
    def test_door_prefilters_audiences_and_markings_before_decide_and_fails_closed(self) -> None:
        decided: list[list[str]] = []

        def decide(handles):
            decided.append(list(handles))
            return {h: None for h in handles}

        # Reader can only read mail:owner and holds no markings.
        self.backend.autopilot_grant = (
            lambda aud: aud == "mail:owner",
            frozenset(),
            decide,
        )
        door = assist_routes._door(self.backend, base.AutopilotArea.client)  # type: ignore[arg-type]
        items = [
            {
                "item_id": "ok_1",
                "audiences": ["mail:owner"],
                "markings": [],
                "citations": ["graph-mail://x/allowed@1"],
            },
            {
                "item_id": "bad_aud",
                "audiences": ["mail:other"],
                "markings": [],
                "citations": ["graph-mail://x/should_not_decide_aud@1"],
            },
            {
                "item_id": "bad_mark",
                "audiences": ["mail:owner"],
                "markings": ["finance"],
                "citations": ["graph-mail://x/should_not_decide_mark@1"],
            },
        ]
        allowed = door(items)
        self.assertEqual(allowed, {"ok_1"})
        # Only the pre-filtered item's handle was sent to decide().
        self.assertEqual(decided, [["graph-mail://x/allowed@1"]])

        # When every item fails pre-filtering, decide() is not called at all.
        decided.clear()
        self.assertEqual(door(items[1:]), set())
        self.assertEqual(decided, [])

        # Fail closed when view.access.grant is missing.
        self.backend.autopilot_grant = None

        class _NoGrantReads:
            def run(self, _reader, job, timeout=60):
                return job(type("V", (), {"access": None})())

        self.backend.reads = _NoGrantReads()
        door_no_grant = assist_routes._door(self.backend, base.AutopilotArea.client)  # type: ignore[arg-type]
        self.assertEqual(door_no_grant(items), set())

    def test_put_off_skips_sqlite_queue_open(self) -> None:
        from operator_api.autopilot_area import DECISIONS_FILE

        dec_path = self.state / DECISIONS_FILE
        dec_path.parent.mkdir(parents=True, exist_ok=True)
        with Journal(dec_path).locked() as held:
            held.append({
                "event": "item_decision",
                "item_id": "a" * 24,
                "decision": "accept",
                "at": datetime.now(timezone.utc).isoformat(),
                "by": base.OWNER[1],
                "matter": True,
            })
        card = m.AutopilotPrepared(
            item_id="a" * 24,
            sender="buyer@example.com",
            channel="mail",
            subject="S",
            kind="todo",
            created_at=NOW.isoformat(),
        )
        with patch.object(self.backend, "_apa_replaced", side_effect=AssertionError("must not open SQLite")):
            got = assist_routes._put_off(self.backend, self.state, [card])
        self.assertEqual(got, [])

    def test_prepared_read_does_not_reopen_queue_for_replaced_map(self) -> None:
        opens = 0
        real_queue = ap.Queue

        def counting_queue(path):
            nonlocal opens
            opens += 1
            return real_queue(path)

        with patch("operating.autopilot.Queue", side_effect=counting_queue):
            r = self.client(base.OWNER).get("/api/autopilot/drafts")
            self.assertEqual(r.status_code, 200, r.text)
        # _prepared_read opens the SQLite queue at most once (plus _ap_visible's single open).
        self.assertLessEqual(opens, 2)


if __name__ == "__main__":
    unittest.main()
