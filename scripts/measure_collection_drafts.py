"""Count what the collection-draft autopilot (``operating.autopilot_collections``) would
prepare on a served store for one day, with the autopilot switched on in memory only (the
shipped registry is not changed). Read-only: nothing is queued, sent or written.

    PLX_ONTOLOGY_PG=only python3 scripts/measure_collection_drafts.py --store <path> \\
        --source <source> --day 2026-11-14 --out <counts.json> [--sample 15]

``--out`` holds counts only (customers, invoices, drafts, holds and skips by reason, tones,
the reads' own skip counts): no amount, customer, address or document number. ``--sample N``
prints N drafts to stdout, for the person hand-checking them against the records (finance
figures stay on the screen, never in a file).
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operating import autopilot as ap  # noqa: E402
from operating import autopilot_queue
from operating import autopilot_reading
from operating import autopilot_collections as ac  # noqa: E402


def measure(st, people, day: date, seconds: float = 60.0, sample: int = 0
            ) -> tuple[dict, list]:
    reg = ap.load_registry()
    for a in reg["autopilots"]:
        if a["id"] == "collection_draft":
            a["enabled"] = True
    spec = next(a for a in reg["autopilots"] if a["id"] == "collection_draft")
    began = time.monotonic()
    events = ac.collection_events(st, reg, day, people)
    counts: Counter = Counter({"customers_past_due": len(events)})
    counts["events_seconds"] = round(time.monotonic() - began)
    drafts = []
    for e in events:
        try:
            got = ap._prepare(st, spec, e, day, (), seconds)
        except autopilot_queue.HandlerTimeout:
            counts["skip:handler_timeout"] += 1
            continue
        if "skip" in got:
            counts["skip:" + got["skip"]] += 1
            continue
        w = got["work"]
        counts["drafts"] += 1
        counts["invoices_drafted"] += len(w["invoices"])
        counts["tone:" + w["tone"]] += 1
        counts["owner_known"] += bool(e.get("owner_hint"))
        for h in w["held"]:
            counts["invoice_held:" + h["reason"]] += 1
        drafts.append(w)
    counts.update({"read:" + k: v for k, v in getattr(st, "collection_counts", {}).items()})
    counts["seconds"] = round(time.monotonic() - began)
    picked = random.Random(day.toordinal()).sample(drafts, min(sample, len(drafts)))
    return dict(sorted(counts.items())), picked


def main(argv=None) -> int:
    from operating import proactive_engine as pe
    p = argparse.ArgumentParser(prog="measure_collection_drafts")
    p.add_argument("--store", required=True)
    p.add_argument("--source", choices=autopilot_reading.STORE_SOURCES, default=None)
    p.add_argument("--day", default=date.today().isoformat())
    p.add_argument("--out", required=True)
    p.add_argument("--sample", type=int, default=0)
    a = p.parse_args(argv)
    people = pe._people(Path(a.store))
    if people is None or getattr(people, "unavailable", None):
        print(json.dumps({"status": "blocked", "reason": "people_exclusion_unavailable"}))
        return 2
    st = ap._store(a.store, a.source)
    try:
        counts, picked = measure(st, people, date.fromisoformat(a.day), sample=a.sample)
    finally:
        st.close()
    Path(a.out).write_text(json.dumps({"day": a.day, "counts": counts}, indent=1))
    print(json.dumps(counts, sort_keys=True))
    for w in picked:
        print("\n---", w["tone"], w["customer"], "from", w["recipient_from"])
        print("invoices:", json.dumps(w["invoices"]))
        print("held:", json.dumps(w["held"]))
        print("To:", w["draft"]["to"], "| Subject:", w["draft"]["subject"])
        print(w["draft"]["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
