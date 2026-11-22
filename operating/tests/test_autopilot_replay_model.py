"""Model-on replays (``operating.autopilot_replay``): the triage model is off unless named,
and every cycle of a replay day reads with it, the drain's too. Evidence: isolated-fixture."""
from __future__ import annotations

import io
import json
import unittest
from datetime import timedelta
from unittest import mock

from operating import autopilot as ap
from operating import autopilot_replay as rp
from operating import proactive_engine as pe
from operating.tests import test_autopilot_replay as trp
from operating.tests.test_autopilot_replay import (DAY, OURS, OURS_OID, REP, SENT, StubAsks, at,
                                                    mail, people)


class Cli(trp.StoreSource):
    def test_triage_model_is_off_unless_named_and_a_model_on_run_says_so(self):
        """``--triage-model``: off (the default, or "none"/"off") passes no judge and the
        result says "off"; a model name passes the live judge for that model and the result
        names it, so a model-on coverage is never read as the rules' alone."""
        from operating import chat_ask_model
        self.scene(1, lambda i: True)
        self.store()
        seen = []

        def fake(*args, **kw):
            seen.append(kw.get("ask_judge"))
            return {"schema": rp.SCHEMA, "status": "complete"}
        for flag, want in ((None, "off"), ("off", "off"), ("none", "off"), ("haiku", "haiku")):
            with self.subTest(flag=flag), mock.patch.object(rp, "replay", fake), \
                    mock.patch.object(chat_ask_model, "claude_classify",
                                      lambda model=None, **kw: StubAsks()), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as said:
                code = rp.main(["--store", str(self.dir / "s.sqlite"), "--source", "sqlite",
                                "--out", str(self.dir / "out"), "--role", f"person:{OURS}",
                                "--mem-max-gb", "0", "--until", at(23)]
                               + (["--triage-model", flag] if flag else []))
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(said.getvalue())["triage_model"], want)
                judge = seen[-1]
                if want == "off":
                    self.assertIsNone(judge)
                else:
                    self.assertIsInstance(judge, chat_ask_model.Judge)
                    self.assertEqual(judge.model, want)



class Drain(trp.WaitingOnYou):
    REQUEST, FYI = trp.WaitingOnYou.REQUEST, trp.WaitingOnYou.FYI

    def test_cardinality_events_left_past_the_day_cycle_are_triaged_by_the_drain_too(self):
        """A replay day's cycle stops at the registry's time limit and the further cycles
        (``autopilot._drain``) handle the rest: they read with the same triage model as live
        cycles do (customer service, fresh days 11-19..20: both day cycles ended partial,
        126 messages read, and 49 of 72 uncovered answers followed a message no reading
        reached that a reply rule takes when read as owed a reply). Here the day cycle handles nothing, so every message waits for the drain:
        each is read once, the model's yes prepared, its no not. N=1/10/60."""
        from operating import chat_ask_model as cam
        real = ap.run_cycle

        def day_cycle_stops_at_once(st, queue, ids, **kw):
            if ids:     # the day's cycle: discovers every event, handles none (time is up)
                reg = kw["registry"]
                kw["registry"] = {**reg, "limits": {**reg["limits"], "cycle_seconds": -1}}
            return real(st, queue, ids, **kw)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.sent(mail(self.w, SENT - timedelta(days=5), sender=OURS, to=(REP,),
                               body="Here are the specs.", subject="Specs", mailbox=OURS_OID,
                               conversation="intro", n=79_000, hour=9), 9, -5)
                for i in range(n):
                    self.thread(i, ask=self.REQUEST if i % 2 == 0 else self.FYI, earlier=False)
                self.store()
                model = StubAsks(yes=(self.REQUEST,))
                st = pe._Store(self.dir / "s.sqlite", threads=1)
                try:
                    with mock.patch.object(ap, "run_cycle", day_cycle_stops_at_once):
                        out = rp.replay(st, people(), role=f"person:{OURS}",
                                        out=self.dir / "out", since=DAY,
                                        until=DAY + timedelta(days=1), roles={},
                                        config=rp.load_config(), environ={},
                                        end=DAY + timedelta(days=10), probe=lambda: 64.0,
                                        ask_judge=cam.Judge(model, batch=25))
                finally:
                    st.close()
                self.assertEqual(out["prepared_by_autopilot"], {"reply_prep": (n + 1) // 2})
                self.assertEqual(len([m for b in model.sent for m in b]), n)



# only this module's tests: each fixture's own run in its module
for _cls, _base in ((Cli, trp.StoreSource), (Drain, trp.WaitingOnYou)):
    for _name in dir(_base):
        if _name.startswith("test") and _name not in _cls.__dict__:
            setattr(_cls, _name, None)
del _cls, _base, _name


if __name__ == "__main__":
    unittest.main()
