"""Builds evals/memory_recall/heldout.json: 55 synthetic follow-up cases (fixed seed), each a
planted earlier turn, distractor turns about the same supplier or the same attribute,
noise, and a paraphrased follow-up. Written once before any run; never tuned on.

    python3 -m evals.memory_recall.build [--seed N] > evals/memory_recall/heldout.json
"""
from __future__ import annotations

import json
import random
import sys

SUPPLIERS = ["Brightwater", "Tidewell", "Larkspur", "Halden Glass", "Orchid Valley", "Pinecrest",
             "Marlow", "Kestrel", "Corvel Plastics", "Ashgrove Labels", "Northmere Chemicals",
             "Dunmore Cartons", "Silverbirch Resins", "Fenwick Closures", "Ridgeway Pumps",
             "Calloway Inks", "Thornbury Films", "Westvale Foams", "Elmstead Tubes",
             "Garrow Metals", "Hollin Seals", "Ivybridge Caps", "Juniper Pouches",
             "Kelso Drums", "Lowther Liners"]
PEOPLE = ["Ayla Rensburg", "Tomas Brecht", "Mira Okafor", "Jonah Pell", "Rhea Vantongeren",
          "Iker Salo", "Petra Lindqvist", "Dev Arora", "Noor Haddad", "Callum Reid"]
CARRIERS = ["Coastal Bridge Logistics", "Northline Freight", "Ridgeback Haulage",
            "Bluefin Express", "Summit Linehaul", "Harbourside Carriers"]
# (attribute, earlier question, earlier answer with {v}, follow-up paraphrases, value maker)
ATTRS = [
    ("forwarder", "Which forwarder is moving the {s} shipment?",
     "The {s} shipment moves with {v} on the Thursday pickup.",
     ["Who was carrying the {s} goods again?", "Remind me of the haulier for {s}.",
      "Which company picks up {s}'s load?"], lambda r: r.choice(CARRIERS)),
    ("contact", "Who at {s} handles artwork changes?",
     "Artwork changes at {s} go through their prepress lead, {v}.",
     ["Who's our artwork person at {s}?", "Like last time, who do I ask at {s} about artwork?",
      "Name of the {s} prepress contact?"], lambda r: r.choice(PEOPLE)),
    ("artwork", "Which artwork version did {s} approve for the 250 ml bottle?",
     "{s} approved artwork version {v} for the 250 ml bottle.",
     ["What version of the artwork did {s} sign off?", "Which artwork rev is approved at {s}?"],
     lambda r: r.choice("BCDEFGH") + str(r.randint(2, 9))),
    ("quarantine", "Why is the {s} resin lot on hold?",
     "The {s} resin lot {v} is in quarantine pending the viscosity retest.",
     ["What lot of {s} resin was quarantined?", "Which {s} lot was waiting on the retest?"],
     lambda r: "RZ-" + str(r.randint(1000, 9999))),
    ("dock", "Which dock receives {s} deliveries?",
     "{s} deliveries come in at dock {v} on the north side.",
     ["Where do {s} trucks unload?", "Which door was it for {s} deliveries?"],
     lambda r: str(r.randint(2, 14))),
    ("pallet", "What pallet pattern does {s} need?",
     "{s} ships on the {v} pallet pattern, five layers high.",
     ["Remind me the pallet pattern for {s}?", "How does {s} want their pallets built?"],
     lambda r: r.choice(["pinwheel", "column-stack", "interlock", "brick", "split-row"])),
    ("certificate", "What's blocking the {s} shipment?",
     "The {s} shipment waits on the certificate of analysis for batch {v}.",
     ["Did we get that missing certificate for the {s} batch?",
      "Which {s} batch was missing its COA?"], lambda r: "LK-" + str(r.randint(100, 999))),
    ("leadtime", "What lead time did {s} quote for the new closures?",
     "{s} quoted {v} weeks lead time for the new closures.",
     ["How many weeks did {s} say for those closures?", "Lead time {s} gave us on closures?"],
     lambda r: str(r.randint(3, 16))),
    ("sample", "Where are the {s} samples?",
     "The {s} samples went to the lab under tracking {v}.",
     ["What tracking number were the {s} samples on?", "How did the {s} samples ship?"],
     lambda r: "1Z" + "".join(r.choice("0123456789ABCDEF") for _ in range(8))),
    ("decision", "Should the {s} reorder ship split or complete?",
     "We agreed the {s} reorder ships {v}, to keep one delivery appointment.",
     ["What did we decide about splitting the {s} reorder?", "Split or not for {s}'s reorder?"],
     lambda r: r.choice(["complete, not split", "split in two drops"])),
]
INSTRUCTIONS = [
    ("From now on always list the quality hold reason first when I ask about a lot.",
     "Where is lot QX-{n}?", "quality hold reason first"),
    ("Going forward, give me dates as weekday and day, like Thu Nov 27.",
     "When does PO {n} arrive?", "weekday and day"),
    ("Always show quantities in cases, not units.", "How much of item I-{n} is on hand?",
     "in cases, not units"),
    ("Never include tracking numbers unless I ask for them.", "Has order {n} shipped?",
     "tracking numbers"),
]
CLARIFY = [("Which orders are late?", "Late against the promised date or the requested date?",
            "the promised date", "Which orders are late this week?"),
           ("Show open receipts", "Receipts expected today or all open ones?",
            "only today's", "Any open receipts?"),
           ("Who supplies the 30 ml dropper?", "The glass bottle or the dropper insert?",
            "the dropper insert", "Who supplies the 30 ml dropper now?")]
COMMITMENTS = [("Remind me to chase {s} about the cullet sample next week.",
                "Anything I said I'd chase?", "{s}"),
               ("I'll follow up with {s} on the revised drawing.",
                "What follow-ups did I promise?", "{s}"),
               ("Don't let me forget to confirm the {s} pickup window.",
                "What was I supposed to confirm?", "{s}")]


def main() -> int:
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 20261113
    rng = random.Random(seed)
    cases, people = [], ["rui", "mae", "ivo", "ola", "tess", "bo"]
    suppliers = SUPPLIERS[:]
    rng.shuffle(suppliers)

    def fact(s, attr):
        name, q, a, probes, make = attr
        v = make(rng)
        return {"question": q.format(s=s), "answer": a.format(s=s, v=v)}, v, \
            rng.choice(probes).format(s=s)

    def distractors(s, attr, n=2):
        """The same supplier with another attribute, the same attribute for another."""
        out = []
        other = rng.choice([a for a in ATTRS if a[0] != attr[0]])
        out.append(fact(s, other)[0])
        out.append(fact(rng.choice([x for x in SUPPLIERS if x != s]), attr)[0])
        return out[:n]

    plan = [("long_conversation", 15), ("cross_session", 15), ("weeks_later", 10),
            ("superseded_fact", 5)]
    k = 0
    for kind, count in plan:
        for _ in range(count):
            k += 1
            s, attr = suppliers[k % len(suppliers)], ATTRS[k % len(ATTRS)]
            planted, v, probe = fact(s, attr)
            person = people[k % len(people)]
            case = {"id": f"{kind}-{k}", "kind": kind, "person": person}
            if kind == "long_conversation":
                turns = rng.randint(40, 70)
                at = rng.randint(2, 8)
                case["history"] = [{**planted, "turn": at, "conversation": f"L{k}"}] + [
                    {**d, "turn": at + 5 + 7 * i, "conversation": f"L{k}"}
                    for i, d in enumerate(distractors(s, attr))]
                case["noise"] = {"conversation": f"L{k}", "turns": turns}
                case["probe"] = {"conversation": f"L{k}", "question": probe, "expect": v}
            elif kind == "superseded_fact":
                old_v = attr[4](rng)
                while old_v == v:
                    old_v = attr[4](rng)
                handle = f"api://rec/{s.replace(' ', '')}-{attr[0]}#row.value"
                old = {"question": planted["question"], "answer": attr[2].format(s=s, v=old_v),
                       "facts": [[attr[2].format(s=s, v=old_v), handle]]}
                new = {"question": planted["question"], "answer": planted["answer"],
                       "facts": [[planted["answer"], handle]]}
                case["history"] = [{**old, "days_ago": rng.randint(15, 30), "conversation": f"F{k}a"},
                                   {**new, "days_ago": rng.randint(2, 10), "conversation": f"F{k}b"}]
                case["noise"] = {"turns": 20}
                case["probe"] = {"conversation": f"F{k}c", "question": probe, "expect": v,
                                 "never": old_v}
            else:
                days = rng.randint(5, 10) if kind == "cross_session" else rng.randint(21, 45)
                case["history"] = [{**planted, "days_ago": days, "conversation": f"S{k}"}] + [
                    {**d, "days_ago": rng.randint(1, days), "conversation": f"D{k}{i}"}
                    for i, d in enumerate(distractors(s, attr))]
                case["noise"] = {"turns": rng.randint(20, 60)}
                case["probe"] = {"conversation": f"P{k}", "question": probe, "expect": v}
            cases.append(case)
    for i, (said, probe, expect) in enumerate(INSTRUCTIONS):
        n = rng.randint(1000, 9999)
        cases.append({"id": f"instruction-{i}", "kind": "standing_instruction",
                      "person": people[i], "history": [{"question": said, "answer": "Understood.",
                                                        "days_ago": rng.randint(3, 30),
                                                        "conversation": f"I{i}"}],
                      "noise": {"turns": 30},
                      "probe": {"conversation": f"IP{i}", "question": probe.format(n=n),
                                "expect": expect}})
    for i, (q, asked, choice, probe) in enumerate(CLARIFY):
        cases.append({"id": f"clarify-{i}", "kind": "preference", "person": people[i + 2],
                      "clarify": {"days_ago": rng.randint(3, 20), "conversation": f"C{i}",
                                  "question": q, "asked": asked, "choice": choice},
                      "noise": {"turns": 25},
                      "probe": {"conversation": f"CP{i}", "question": probe, "expect": choice}})
    for i, (said, probe, expect) in enumerate(COMMITMENTS):
        s = suppliers[-1 - i]
        cases.append({"id": f"commitment-{i}", "kind": "commitment", "person": people[i + 3],
                      "history": [{"question": said.format(s=s), "answer": "Noted.",
                                   "days_ago": rng.randint(1, 6), "conversation": f"M{i}"}],
                      "noise": {"turns": 15},
                      "probe": {"conversation": f"MP{i}", "question": probe,
                                "expect": expect.format(s=s)}})
    leaks = [{"person": "zed", "question": c["probe"]["question"],
              "answer": f"Zed's own note: answer is Quillon-{n}."}
             for n, c in enumerate(cases[:20])]
    json.dump({"format": "plx-memory-recall/v2",
               "note": f"Generated by evals.memory_recall.build (seed {seed}) before the first "
                       "run; never tuned on. Distractors: the same supplier with another "
                       "attribute, the same attribute for another supplier.",
               "cases": cases, "leaks": leaks}, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
