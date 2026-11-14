"""Recall eval of each person's memory (``operator_api.memory``) over a scratch Postgres.

Each case plays a person's earlier turns, noise turns between them, then one follow-up
question that needs the earlier context, and checks what reaches the agent:

* ``none``: the question alone (what the Ask agent was given before memory),
* ``work_log``: the work log's own recall (``work_log.recall``: words over the last days),
* ``context``: memory's start-of-turn context (no tool call),
* ``context+tool``: that, or what one ``plx.memory`` search for the question returns.

A hit holds the case's ``expect`` (and never its ``never``: a superseded value). Every
probe's context and tool output is also checked for the other person's turns (``leaks``:
any is a failure: their marker word), and the context read is timed.

``--real DSN``: also real questions people asked (``search.query_log``, read only): each
asker's questions replayed in order (a pause over 30 minutes starts a new conversation),
and each later question naming a code (a number of four characters or more) an earlier
conversation of theirs named is a follow-up whose hit is that earlier question's log row
in what reaches the agent (``work_log``: the work log's recall returning it). Only counts
are printed.

    python3 -m evals.memory_recall.run [--cases evals/memory_recall/heldout.json] [--real DSN]

Prints one JSON summary (counts, rates, milliseconds); no case text.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
NOISE = ("Status of purchase order {n}?", "Where is lot N{n}?", "Who supplies part P-{n}?",
         "Is sales order {n} shipped?", "Which receipts came in for PO {n} today?",
         "Open quality holds on line {m}?", "When was item I-{n} last counted?",
         "What is the lead time for component C-{n}?")
NOISE_ANSWER = ("Record {n} is on track; the next step is the receipt at dock {m}, and the "
                "supplier confirmed the date in yesterday's mail.")


class Guard:
    """The people exclusion of the eval's people (none excluded)."""
    unavailable = None

    def is_excluded(self, value: str) -> bool:
        return False

    def decides(self, address: str) -> bool:
        return True

    def known_domains(self) -> set[str]:
        return {"plx.example"}


def person(who: str) -> Any:
    return type("R", (), {"key": f"plx:staff:{who}", "display_name": who,
                          "email": f"{who}@plx.example",
                          "session": type("S", (), {"identities": frozenset(),
                                                    "subject_oid": None,
                                                    "view_as": False})()})()


def answer(ident: str, question: str, said: str, conversation: str, *, facts=(),
           subjects=()) -> dict[str, Any]:
    return {"answer_id": ident, "question": question, "direct_answer": said,
            "outcome": "answered", "conversation_id": conversation,
            "facts": [{"text": t, "check": "passed",
                       "sources": [{"handle": h, "label": "record"}]} for t, h in facts],
            "subjects": [{"object_id": s} for s in subjects]}


def play(store: Any, work: Any, case: dict[str, Any], rng: random.Random) -> None:
    from operator_api import work_log as wl
    who = person(case["person"])
    noise = case.get("noise") or {}
    turns: list[tuple[datetime, dict[str, Any]]] = []
    count = int(noise.get("turns") or 0)
    if case["kind"] == "long_conversation":
        start = NOW - timedelta(hours=6)
        step = timedelta(hours=6) / (count + 2)
        planted = {h["turn"]: h for h in case.get("history") or ()}
        for n in range(1, count + 1):
            at = start + step * n
            if n in planted:
                h = planted[n]
                turns.append((at, answer(f"{case['id']}-h{n}", h["question"], h["answer"],
                                         h["conversation"], subjects=h.get("subjects") or ())))
            else:
                k = rng.randrange(10000, 99999)
                turns.append((at, answer(f"{case['id']}-n{n}",
                                         rng.choice(NOISE).format(n=k, m=k % 9),
                                         NOISE_ANSWER.format(n=k, m=k % 9),
                                         noise.get("conversation") or "noise")))
    else:
        for n, h in enumerate(case.get("history") or ()):
            turns.append((NOW - timedelta(days=h["days_ago"]), answer(
                f"{case['id']}-h{n}", h["question"], h["answer"], h["conversation"],
                facts=[tuple(f) for f in h.get("facts") or ()],
                subjects=h.get("subjects") or ())))
        oldest = max([h["days_ago"] for h in case.get("history") or ()]
                     + [(case.get("clarify") or {}).get("days_ago", 1)])
        for n in range(count):
            k = rng.randrange(10000, 99999)
            turns.append((NOW - timedelta(days=rng.uniform(0.1, oldest)), answer(
                f"{case['id']}-n{n}", rng.choice(NOISE).format(n=k, m=k % 9),
                NOISE_ANSWER.format(n=k, m=k % 9), f"{case['id']}-noise{n % 7}")))
    for at, data in sorted(turns, key=lambda t: t[0]):
        store.clock = lambda at=at: at
        work.clock = lambda at=at: at
        store.from_answer(who, data)
        wl.Notes(work, lambda r: None).ask(who, data)
    clarify = case.get("clarify")
    if clarify:
        store.clock = lambda: NOW - timedelta(days=clarify["days_ago"])
        store.from_clarify(who, clarify["question"], clarify["asked"], clarify["choice"],
                           f"{case['id']}-c", clarify["conversation"])
    store.clock = lambda: NOW
    work.clock = lambda: NOW


_CODE = None


def real_cases(dsn: str) -> list[list[dict[str, Any]]]:
    """Each asker's questions from ``search.query_log`` as conversations (module doc)."""
    import psycopg
    import re
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        rows = conn.execute("SELECT actor, at, question FROM search.query_log WHERE actor IS "
                            "NOT NULL AND question IS NOT NULL ORDER BY actor, at").fetchall()
    people: dict[str, list[dict[str, Any]]] = {}
    for actor, at, question in rows:
        turns = people.setdefault(actor, [])
        n = 0 if not turns else turns[-1]["n"] + (
            (at - turns[-1]["at"]).total_seconds() > 1800)
        turns.append({"at": at, "question": question.strip()[:1000], "n": n,
                      "conversation": f"r{len(people)}-{n}",
                      "codes": set(re.findall(r"\b[A-Za-z]{0,6}-?\d{4,}[A-Za-z0-9-]*\b",
                                              question))})
    return list(people.values())


def run_real(store: Any, work: Any, dsn: str) -> dict[str, Any]:
    from operator_api import work_log as wl
    hits = {"work_log": 0, "context": 0, "context+tool": 0}
    probes, took = 0, []
    for n, turns in enumerate(real_cases(dsn)):
        who = person(f"real{n}")
        seen: dict[str, tuple[int, str, str]] = {}   # code -> (log id, answer id, conversation)
        for i, turn in enumerate(turns):
            at = turn["at"]
            target = next((seen[c] for c in turn["codes"] if c in seen
                           and seen[c][2] != turn["conversation"]), None)
            if target is not None:
                store.clock = lambda at=at: at
                work.clock = lambda at=at: at
                probes += 1
                started = time.perf_counter()
                context = store.context(who, turn["question"],
                                        conversation=turn["conversation"])
                took.append((time.perf_counter() - started) * 1000)
                tool = json.dumps(store.tool(who).call({"query": turn["question"]}))
                mark = f"#{target[0]}, "   # a log row as the context names it (``say``)
                found = wl.recall(work, who.key, turn["question"])
                hits["work_log"] += any(e.get("source_id") == target[1]
                                        for e in found["events"])
                hits["context"] += mark in context or f"#{target[0]} " in context
                hits["context+tool"] += mark in context or f'"log_id": {target[0]},' in tool \
                    or f"#{target[0]} " in context
            data = answer(f"real{n}-{i}", turn["question"], "", turn["conversation"])
            store.clock = lambda at=at: at
            work.clock = lambda at=at: at
            ids = store.from_answer(who, data)
            wl.Notes(work, lambda r: None).ask(who, data)
            for code in turn["codes"]:
                if ids:
                    seen[code] = (ids[0], data["answer_id"], turn["conversation"])
    return {"probes": probes, "rates": {k: round(v / probes, 3) if probes else None
                                        for k, v in hits.items()},
            "context_ms": {"p50": round(statistics.median(took), 1) if took else None,
                           "max": round(max(took), 1) if took else None}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m evals.memory_recall.run")
    parser.add_argument("--cases", default=str(HERE / "heldout.json"))
    parser.add_argument("--real", default=None, help="the Postgres holding search.query_log")
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.cases).read_text("utf-8"))
    import psycopg
    from operating.pg import migrate, scratch_db
    from operator_api import memory as mem
    from operator_api import work_log as wl
    scratch_db.use_test_cluster()
    name, dsn = scratch_db.create_db()
    store = None
    try:
        with psycopg.connect(dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})
        # the eval's people carry no excluded name (none is excluded)
        store = mem.Memory.for_dsn(dsn, sync=True, guard=Guard, names=lambda: frozenset())
        work = wl.WorkLog(None, guard=Guard)
        rng = random.Random(1113)
        for leak in spec.get("leaks") or ():
            store.from_answer(person(leak["person"]), answer(
                f"leak-{leak['person']}-{rng.random()}", leak["question"], leak["answer"],
                "leak"))
        for case in spec["cases"]:
            play(store, work, case, rng)
        leak_words = sorted({leak.get("marker") or "Quillon" for leak in spec.get("leaks") or ()})
        rows, took, leaks = [], [], 0
        for case in spec["cases"]:
            who, probe = person(case["person"]), case["probe"]
            started = time.perf_counter()
            context = store.context(who, probe["question"], conversation=probe["conversation"],
                                    records=probe.get("records") or ())
            took.append((time.perf_counter() - started) * 1000)
            # what the tool returns as current (an item it marks superseded is history)
            tool = json.dumps([i for i in store.tool(who).call(
                {"query": probe["question"], **({"records": probe["records"]}
                                               if probe.get("records") else {})})["items"]
                if not i.get("superseded")])
            found = wl.recall(work, who.key, probe["question"])
            logged = " ".join(f"{e['title']} {e.get('detail') or ''}"
                              for e in found["events"])
            expect, never = probe["expect"], probe.get("never")

            def said(value: str, text: str) -> bool:
                # as a word: "5" is not said by "#15" or "2026-11-05"
                return re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])",
                                 text) is not None

            def hit(text: str) -> bool:
                return said(expect, text) and not (never and said(never, text))
            leaks += sum(1 for w in leak_words if w in context or w in tool)
            rows.append({"id": case["id"], "kind": case["kind"],
                         "none": hit(probe["question"]), "work_log": hit(logged),
                         "context": hit(context), "context+tool": hit(context) or hit(tool)})
        n = len(rows)
        arms = ("none", "work_log", "context", "context+tool")
        kinds = sorted({r["kind"] for r in rows})
        out = {"cases": n, "file": Path(args.cases).name,
               "rates": {c: round(sum(r[c] for r in rows) / n, 3) for c in arms},
               "by_kind": {k: {"n": sum(1 for r in rows if r["kind"] == k),
                               **{c: round(sum(r[c] for r in rows if r["kind"] == k)
                                           / sum(1 for r in rows if r["kind"] == k), 3)
                                  for c in arms[1:]}} for k in kinds},
               "missed": [r["id"] for r in rows if not r["context+tool"]],
               "leaks": leaks,
               "context_ms": {"p50": round(statistics.median(took), 1),
                              "max": round(max(took), 1)}}
        if args.real:
            out["real"] = run_real(store, work, args.real)
        print(json.dumps(out, indent=1))
        return 0 if leaks == 0 else 1
    finally:
        if store is not None:
            store.pool.close()
        scratch_db.drop_db(name)


if __name__ == "__main__":
    sys.exit(main())
