"""Precedents (``operating.assist_precedents``) on Postgres with made-up comms rows: the act is
read from who the send went to against who it answered (reply, forward, new_message, a chat reply),
a matter the person set aside is a 'nothing', a system act a system_write; built once; every
situation embedded once; ``nearest`` returns EVERY precedent before the basis (the fence), same
party first, no limit, colleagues' only when the person's own are thin; the person's total is
said; a person with no acts has no precedents (nothing is built for anyone else).
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import unittest

from operating import assist_precedents as ap
from operating.tests import test_assist_matters as base

NOW, ANN, BOB, OUT = base.NOW, base.ANN, base.BOB, base.OUT
DIM = 1024


def vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


class Embedder:
    def __init__(self):
        self.seen = []

    def embed(self, texts):
        self.seen.extend(texts)
        return [vec(sum(map(ord, t)) % 50) for t in texts]


class Precedents(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    mail = base.Matters.mail
    biz_tables = base.Matters.biz_tables

    def setUp(self):
        base.Matters.setUp(self)
        self.pg.execute("CREATE EXTENSION IF NOT EXISTS vector")       # the Spark test cluster has it
        self.pg.execute("DROP TABLE IF EXISTS assist.precedent_vectors")
        from operating.pg.migrate import MIGRATIONS_DIR
        self.pg.execute((MIGRATIONS_DIR / "0072_precedent_vectors.sql").read_text("utf-8"))

    def act(self, key, person, at, thread, to, *, system="mail", kind="mail_sent", handle=""):
        self.pg.execute(
            "INSERT INTO assist.actions (action_key, person, at, system, kind, target_handle, "
            "thread_key, recipients, attributed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
            "'sender')", (key, person, at, system, kind, handle, thread, to))

    def build(self, own="Glenn, can you send this?", asked="Please send the COA for lot 7"):
        return ap.build(self.pg, lambda sends: {s["action_key"]: {"own": own, "trigger": asked}
                                                for s in sends},
                        roles={ANN: "quality", BOB: "quality"})

    def got(self):
        return {r[0]: r[1:] for r in self.pg.execute(
            "SELECT action_key, act, trigger_kind, channel, act_to FROM assist.precedents")}

    def test_the_act_is_read_from_who_the_send_went_to(self):
        t0 = NOW - dt.timedelta(days=3)
        self.mail("th1", OUT, ["ann@ours.example"], at=t0)                 # asks Ann
        self.mail("th2", OUT, ["ann@ours.example"], at=t0)
        self.mail("th3", OUT, ["ann@ours.example"], at=t0)
        later = t0 + dt.timedelta(hours=1)
        self.act("a-reply", ANN, later, "th1", [OUT])                       # answers the sender
        self.act("a-fwd", ANN, later, "th2", ["glenn@ours.example"])        # passes it on
        self.act("a-new", ANN, later, "th-none", ["x@ours.example"])        # nobody wrote before
        self.pg.execute("INSERT INTO assist.actions (action_key, person, at, system, kind, "
                        "thread_key, attributed_by, not_human) VALUES ('a-tpl', %s, %s, 'mail', "
                        "'mail_sent', 'th3', 'sender', 'system_generated')", (ANN, later))
        out = self.build()
        self.assertEqual(out["candidates"], 3)                              # the template is none
        got = self.got()
        self.assertEqual(got["a-reply"][:2], ("reply", "message"))
        self.assertEqual(got["a-fwd"][:2], ("forward", "message"))
        self.assertEqual(got["a-new"][:2], ("new_message", "none"))
        self.assertEqual(self.build()["candidates"], 0)                     # built once
        self.assertEqual(len(self.got()), 3)

    def test_the_situation_is_what_was_asked_not_what_the_person_answered(self):
        t0 = NOW - dt.timedelta(days=3)
        h = self.mail("thS", OUT, ["ann@ours.example"], at=t0)
        self.act("a-s", ANN, t0 + dt.timedelta(hours=1), "thS", [OUT])
        self.build(own="Sent it.", asked="Please send the COA for lot 7")
        sit, words, sha = self.pg.execute("SELECT situation, act_text, situation_sha FROM "
                                          "assist.precedents WHERE action_key = 'a-s'").fetchone()
        self.assertIn("Please send the COA for lot 7", sit)
        self.assertNotIn("Sent it.", sit)
        self.assertEqual((words, sha), ("Sent it.", ap.sha_of(sit)))
        self.assertTrue(h)
        # with a reading of the trigger, the reading's summary and ask are what is embedded
        self.pg.execute("INSERT INTO assist.precedents SELECT * FROM assist.precedents WHERE false")
        self.pg.execute("TRUNCATE assist.precedents")
        key = self.pg.execute("SELECT message_id FROM biz.emails WHERE thread = 'thS'").fetchone()[0]
        self.pg.execute("INSERT INTO assist.readings (source, message_key, handles, thread_key, said_at, "
                        "read_ver, needs_act, act, act_class, summary, quote, status, actor_person, "
                        "actor_basis, counterpart) VALUES ('emails', %s, %s, 'thS', %s, 2, true, "
                        "'send the COA', 'send_document', 'Asks for a COA', 'q', 'ok', %s, 'address', "
                        "'Croda')", (key, [h], t0, ANN))
        self.build()
        sit, cp = self.pg.execute("SELECT situation, counterpart FROM assist.precedents WHERE "
                                  "action_key = 'a-s'").fetchone()
        self.assertEqual((sit, cp), ("Asks for a COA | asks: send the COA | Croda", "Croda"))

    def test_system_acts_and_a_set_aside_matter(self):
        self.act("sage:1", BOB, NOW, None, [], system="sage", kind="sage_ar_invoice")
        self.act("mc:1", BOB, NOW, None, [], system="mission_control", kind="approval")
        self.pg.execute(
            "INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, sources, "
            "basis_at, title, state, done_reason, done_at) VALUES ('m1', %s, 'conversation', "
            "'th9', '{}', %s, 'Asks about COA', 'dropped', 'person_dropped', %s)",
            (ANN, NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=1)))
        self.build()
        got = self.got()
        self.assertEqual(got["sage:1"], ("system_write", "none", "system", ["sage:sage_ar_invoice"]))
        self.assertEqual(got["mc:1"][0], "system_write")
        self.assertEqual(got["mc:1"][3], ["mc:approval_decide"])
        self.assertEqual(got["none:m1"][:2], ("nothing", "none"))

    def test_every_situation_is_embedded_once(self):
        self.act("a1", ANN, NOW, "th-n", ["x@ours.example"])
        self.act("a2", ANN, NOW + dt.timedelta(minutes=1), "th-n2", ["x@ours.example"])
        self.build()
        emb = Embedder()
        self.assertEqual(ap.embed(self.pg, emb, "stand-in"), 1)           # same words: one situation
        self.assertEqual(ap.embed(self.pg, emb, "stand-in"), 0)
        self.assertEqual(len(emb.seen), 1)

    def test_nearest_is_the_fenced_whole_history_same_party_first_with_colleagues_when_thin(self):
        base_at = NOW - dt.timedelta(days=100)
        rows = []
        for i in range(45):          # Ann's own history: 45 cases, far more than any old limit
            rows.append((f"own{i}", ANN, base_at + dt.timedelta(days=i), f"s{i}", "Croda" if i == 7 else ""))
        for key, person, at, sit, cp in rows:
            self.pg.execute(
                "INSERT INTO assist.precedents (action_key, person, role, at, trigger_kind, channel, "
                "counterpart, act, situation, situation_sha) VALUES (%s, %s, 'quality', %s, 'none', "
                "'email', %s, 'reply', %s, %s)", (key, person, at, cp, sit, ap.sha_of(sit)))
            self.pg.execute("INSERT INTO assist.precedent_vectors VALUES (%s, 'm', %s::halfvec)",
                            (ap.sha_of(sit), "[" + ",".join(map(str, vec(int(sit[1:])))) + "]"))
        basis = base_at + dt.timedelta(days=30)
        rows, total = ap.nearest(self.pg, ANN, "quality", basis, vec(3), model="m",
                                 counterpart="Croda")
        self.assertEqual(total, 45)
        self.assertEqual(len(rows), 30)                         # the fence: only days 0..29
        self.assertTrue(all(r["at"] < basis for r in rows))
        self.assertEqual(rows[0]["action_key"], "own7")         # the same party first, though far
        self.assertEqual(rows[1]["action_key"], "own3")         # then by similarity
        self.assertGreater(len(rows), 6)                        # no limit
        # a thin history: colleagues' cases, marked
        self.pg.execute("INSERT INTO assist.precedents (action_key, person, role, at, trigger_kind, "
                        "channel, act, situation, situation_sha) VALUES ('bob1', %s, 'quality', %s, "
                        "'none', 'email', 'forward', 'b1', %s)", (BOB, base_at, ap.sha_of("b1")))
        self.pg.execute("INSERT INTO assist.precedent_vectors VALUES (%s, 'm', %s::halfvec)",
                        (ap.sha_of("b1"), "[" + ",".join(map(str, vec(3))) + "]"))
        thin, _ = ap.nearest(self.pg, ANN, "quality", base_at + dt.timedelta(days=10), vec(3),
                             model="m")
        self.assertIn("colleague", {r["why"] for r in thin})
        full, _ = ap.nearest(self.pg, ANN, "quality", basis, vec(3), model="m")
        self.assertNotIn("colleague", {r["why"] for r in full})   # 30 of their own: not thin
        none, total0 = ap.nearest(self.pg, "p-nobody", "", basis, vec(3), model="m")
        self.assertEqual((none, total0), ([], 0))


    def seed(self, n_own, base_at):
        for i in range(n_own):
            sit = f"s{i}"
            self.pg.execute(
                "INSERT INTO assist.precedents (action_key, person, role, at, trigger_kind, channel, "
                "act, situation, situation_sha) VALUES (%s, %s, 'quality', %s, 'none', 'email', 'reply', "
                "%s, %s) ON CONFLICT DO NOTHING", (f"a{i}", ANN, base_at + dt.timedelta(days=i), sit, ap.sha_of(sit)))
            self.pg.execute("INSERT INTO assist.precedent_vectors VALUES (%s, 'm', %s::halfvec) "
                            "ON CONFLICT DO NOTHING", (ap.sha_of(sit), "[" + ",".join(map(str, vec(i))) + "]"))

    def test_nearest_many_answers_every_ask_in_one_statement_each_with_its_own_fence(self):
        base_at = NOW - dt.timedelta(days=100)
        self.seed(45, base_at)
        for n in (1, 10, 60):
            counting = base.Counting(self.pg)
            asks = [{"id": f"m{i}", "person": ANN, "role": "quality", "vector": vec(i),
                     "basis_at": base_at + dt.timedelta(days=5 + i)} for i in range(n)]
            asks.append({"id": "nobody", "person": "p-nobody", "role": "", "vector": vec(1), "basis_at": NOW})
            got = ap.nearest_many(counting, asks, model="m")
            self.assertEqual(set(got), {a["id"] for a in asks})          # no ask lost or duplicated
            self.assertEqual(counting.n, 2, n)                           # the lateral + the totals: not per ask
            for a in asks[:-1]:
                rows, total = got[a["id"]]
                self.assertEqual(total, 45)
                self.assertTrue(all(r["at"] < a["basis_at"] for r in rows))
                self.assertEqual(len(rows), min(45, 5 + int(a["id"][1:])))   # the whole fenced history
                one, _ = ap.nearest(self.pg, ANN, "quality", a["basis_at"], a["vector"], model="m")
                self.assertEqual([r["action_key"] for r in rows], [r["action_key"] for r in one])
            self.assertEqual(got["nobody"], ([], 0))
        with self.assertRaises(ValueError):
            ap.nearest_many(self.pg, [asks[0], asks[0]], model="m")

    def test_as5_built_passes_only_when_every_busy_person_has_enough_precedents_and_the_fence_holds(self):
        import io
        from contextlib import redirect_stdout
        from operating import assist_checks as ac
        base_at = NOW - dt.timedelta(days=100)

        def check():
            out = io.StringIO()
            with redirect_stdout(out):
                rc, _ = ac.as5_built(self.pg)
            return rc, out.getvalue()
        rc, said = check()
        self.assertEqual(rc, 2, said)                                    # nothing built: never a pass on nothing
        for i in range(31):
            self.act(f"s{i}", ANN, NOW - dt.timedelta(days=i), f"t{i}", [OUT])
        rc, said = check()
        self.assertEqual((rc, said.startswith("as5_built 0/1 fail")), (1, True), said)
        self.seed(31, base_at)
        self.pg.execute("INSERT INTO assist.matters (matter_id, owner_person, kind, subject_key, state, title, detail, "
                        "basis_at, sources) SELECT 'm1', %s, 'conversation', 'k', 'open', 't', 'd', %s, '{}'::jsonb", (ANN, base_at + dt.timedelta(days=10)))
        rc, said = check()
        self.assertEqual(rc, 0, said)
        self.assertIn("as5_built 1/1 pass", said)
        self.assertIn("0 precedents dated at or after their basis", said)
        self.assertIn("precedents by act", said)


if __name__ == "__main__":
    unittest.main()
