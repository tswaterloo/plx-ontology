"""The Assist scoreboard: every person counted once (ASSIST plan piece 1).

``day_coverage.measure`` scores one role at a time, and a person who belongs to several
roles (the same two people's Sage invoices sit in customer service and receivables) is
counted in each: the role totals were not additive. This reads those per-role results and
makes the person the unit:

* a person's actions are counted once, however many roles' results hold them (by
  ``day_coverage.action_key``);
* an action is covered when ANY of the person's roles' replay queues anticipated it (the
  person would see every item prepared for them);
* the person's row goes to one primary role: the role whose queue prepared the most items
  for the person, ties to the alphabetically first (stated in the output, never guessed
  from names); a person with no members' result is not invented.

What is NOT here, and is said in the output (``not_measured``): the second to fourth tiers
(right act, usable content, on time) need the judged pairs and the shadow queue; hours
(``MINUTES`` is an estimate per kind, so none are reported); the matter staleness. The result holds ids,
kinds and counts only.

    python3 -m operating.assist_scoreboard --out FILE ROLE_RESULT.json [ROLE_RESULT.json ...]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

SCHEMA = "plx-assist-scoreboard/v1"
NOT_MEASURED = ["right_act", "usable_content", "on_time", "hours", "open_item_staleness",
                "anticipatable_label"]


def board(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    done = [r for r in results if r.get("status") == "complete" and "people_actions" in r]
    skipped = [{"role": r.get("role"), "status": r.get("status"),
                "reason": r.get("reason") or ("no people_actions in the result"
                                              if r.get("status") == "complete" else "")}
               for r in results if r not in done]
    actions: dict[str, dict[str, tuple[str, int]]] = defaultdict(dict)   # person -> key -> (type, covered)
    roles_of: dict[str, set[str]] = defaultdict(set)
    items: dict[str, Counter] = defaultdict(Counter)                     # person -> role -> items
    for r in done:
        role = str(r["role"])
        for person in r.get("member_ids") or ():
            roles_of[person].add(role)
        for person, n in (r.get("items_by_person") or {}).items():
            items[person][role] += n
        for person, rows in r["people_actions"].items():
            roles_of[person].add(role)
            for key, typ, hit in rows:
                old = actions[person].get(key)
                actions[person][key] = (typ, max(hit, old[1]) if old else hit)
    people: dict[str, Any] = {}
    for person in sorted(set(actions) | set(roles_of)):
        mine = actions.get(person, {})
        rs = sorted(roles_of[person])
        primary = sorted(rs, key=lambda role: (-items[person][role], role))[0] if rs else None
        by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for typ, hit in mine.values():
            by_type[typ][0] += 1
            by_type[typ][1] += hit
        people[person] = {"primary_role": primary, "roles": rs, "actions": len(mine),
                          "covered": sum(h for _t, h in mine.values()),
                          "types": {t: {"actions": a, "covered": c} for t, (a, c) in sorted(by_type.items())}}
    roles: dict[str, Counter] = defaultdict(Counter)
    for p in people.values():
        if p["primary_role"]:
            roles[p["primary_role"]]["people"] += 1
            roles[p["primary_role"]]["actions"] += p["actions"]
            roles[p["primary_role"]]["covered"] += p["covered"]
    raw = {str(r["role"]): {"actions": (r.get("work") or {}).get("actions"),
                            "covered": (r.get("work") or {}).get("covered")} for r in done}
    total_a = sum(p["actions"] for p in people.values())
    total_c = sum(p["covered"] for p in people.values())
    return {"schema": SCHEMA,
            "assignment_rule": "primary role = the role whose queue prepared the most items for the "
                               "person (ties: first by name); a person's action is covered when any "
                               "of their roles' queues anticipated it; counted once",
            "people": people, "roles": {k: dict(v) for k, v in sorted(roles.items())},
            "totals": {"people": len(people), "actions": total_a, "covered": total_c},
            "old_role_totals": raw,
            "old_role_sum": {"actions": sum((v["actions"] or 0) for v in raw.values()),
                             "covered": sum((v["covered"] or 0) for v in raw.values())},
            "not_measured": NOT_MEASURED, "skipped": skipped}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_scoreboard")
    p.add_argument("--out", required=True)
    p.add_argument("results", nargs="+")
    a = p.parse_args(argv)
    loaded = []
    for path in a.results:
        try:
            got = json.loads(Path(path).read_text())
            loaded.extend(got if isinstance(got, list) else [got])     # the re-score writes a one-item list
        except (OSError, ValueError) as exc:
            loaded.append({"role": path, "status": "blocked", "reason": f"unreadable: {type(exc).__name__}"})
    out = board(loaded)
    Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=True))
    print(json.dumps({"totals": out["totals"], "roles": out["roles"], "old_role_sum": out["old_role_sum"],
                      "skipped": out["skipped"]}))
    return 1 if out["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
