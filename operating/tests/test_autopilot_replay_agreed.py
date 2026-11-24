"""The replay's score with a judge (the scoring audit 11-22): a send covers an item only when
what was really sent does the prepared work."""
from datetime import datetime, timedelta, timezone
import unittest

from operating import autopilot as ap
from operating import autopilot_replay as rp


class Agreed(unittest.TestCase):
    def test_cardinality_with_a_judge_only_sends_that_do_the_prepared_work_cover(self):
        """The scoring audit (11-22): without ``agrees`` the first later action in the
        thread matches (a "thanks" covers, a Teams burst after it is credited). With it,
        each candidate (at most ``MAX_CANDIDATES``, in time order) counts only when the
        judge says yes; an unjudged pair (None) covers nothing; no burst credit. N=1/10/60
        items: every candidate is collected once, none lost or doubled."""
        utc = timezone.utc
        at = datetime(2026, 10, 20, 13, tzinfo=utc)
        args = dict(horizon=timedelta(hours=24), data_end=at + timedelta(days=5),
                    since=at - timedelta(days=1), until=at + timedelta(days=1))
        for n in (1, 10, 60):
            events = {f"e{i}": {"event_type": "message.received", "channel": "teams",
                                "conversation_id": f"c{i}", "at": ap._iso(at)} for i in range(n)}
            items = [{"event_id": f"e{i}", "item_id": f"i{i}", "autopilot": "teams_reply",
                      "owner": "ana", "created_at": ap._iso(at), "body": {}} for i in range(n)]
            acts = []
            for i in range(n):
                for k, what in enumerate(("thanks", "answer", "more")):
                    acts.append({"person": "ana", "kind": "teams_sent", "workflow": "teams_chat",
                                 "conversation": f"c{i}", "what": what, "other_at": None,
                                 "at": ap._iso(at + timedelta(minutes=30 + 2 * k))})
            loose: set = set()
            rp.score(items, events, acts, anticipated=loose, **args)
            self.assertEqual(len(loose), 3 * n)          # thanks matched, the burst credited
            seen: list = []
            judged: set = set()
            verdict = {"thanks": False, "answer": True, "more": None}
            flows = rp.score(items, events, acts, anticipated=judged, candidates=seen,
                             agrees=lambda item, a: verdict[a["what"]], **args)
            self.assertEqual(len(seen), 3 * n)
            self.assertEqual(len({(i["item_id"], id(a)) for i, _e, a in seen}), 3 * n)
            self.assertEqual({a["what"] for a in acts if id(a) in judged}, {"answer"})
            self.assertEqual(len(judged), n)
            self.assertEqual(flows["teams_chat"]["matched"], n)
            none = rp.score(items, events, acts, agrees=lambda item, a: None, **args)
            self.assertEqual(none["teams_chat"]["matched"], 0)
            self.assertEqual(none["teams_chat"]["anticipated"], 0)


if __name__ == "__main__":
    unittest.main()
