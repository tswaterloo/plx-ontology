"""Yesterday's Assist scoreboard (ASSIST plan piece 1): what the live queue had prepared for
each person before the person acted, per person once (``assist_scoreboard``).

The code is fixed before the day happens, so every night is a day nobody tuned on. What it
scores is the LIVE queue (``autopilot.sqlite``): a person with Assist switched off has no
items, so their number is the floor, not a measure of what Assist could do; the output says
how many people had any live item. Matching is ``same_thread_or_record`` (an upper bound: an
item and a later act on the same conversation or record), because no judge has seen the
day's pairs; ``matching`` is in every role's result. Counts, ids and kinds only.

    python3 -m operating.assist_nightly --store STORE [--source postgres] [--day YYYY-MM-DD]
        [--state-dir /data/plx-state] [--out-dir DIR]
Writes ``<out-dir>/<day>.json``; exit 1 when any role was blocked or the board skipped one.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Sequence

from . import assist_scoreboard as sb
from . import autopilot as ap
from . import autopilot_queue
from . import autopilot_replay as rp
from . import day_coverage as dc

ROLES_DEFAULT = "work-graph/roles-derived.json"     # under the state dir: the file the live autopilot reads


def window(day: date) -> tuple[datetime, datetime]:
    """The business day [00:00, 24:00) US Eastern as UTC instants."""
    lo = datetime(day.year, day.month, day.day, tzinfo=dc.DAY_TZ)
    hi = datetime(day.year, day.month, day.day, tzinfo=dc.DAY_TZ) + timedelta(days=1)
    return lo.astimezone(timezone.utc), hi.astimezone(timezone.utc)


def run(store: str, source: str | None, day: date, state_dir: Path, roles_file: Path,
        persons: Sequence[str] = ()) -> dict[str, Any]:
    """``persons``: addresses scored as their own role (``person:<address>``, the replay's
    per-person scope) beside the derived roles: the people whose work no role stands for."""
    from . import proactive_engine as pe
    saved = json.loads(roles_file.read_text())
    roles = {k: list(v) for k, v in (saved.get("roles") or {}).items() if v}
    for person in persons:
        roles.setdefault(f"person:{person.strip().lower()}", [])
    since, until = window(day)
    st = ap._store(store, source)
    people = pe._people(Path(store))
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="assist-nightly-") as tmp:
        live = state_dir / "autopilot" / ap.QUEUE_FILE
        if not live.exists():
            return {"schema": sb.SCHEMA, "status": "blocked", "reason": "no_live_queue"}
        for role in sorted(roles):
            if role.startswith(autopilot_queue.POOL_PREFIX):
                continue
            slug = "".join(c if c.isalnum() or c == "_" else "_" for c in role.lower())
            root = Path(tmp) / slug
            root.mkdir()
            # a consistent copy of the live queue (SQLite's own backup: the file is in use)
            src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
            dst = sqlite3.connect(root / ap.QUEUE_FILE)
            src.backup(dst)
            src.close()
            dst.close()
            (root / autopilot_queue.ROLES_FILE).write_text(json.dumps({"roles": roles}))
            results.append(dc.measure(st, people, role=role, replay_dir=Path(tmp), since=since,
                                      until=until, roles=roles, config=rp.load_config(),
                                      cache=None))
    st.close()
    out = sb.board(results)
    out["day"] = day.isoformat()
    out["matching"] = "same_thread_or_record (upper bound; live queue only)"
    out["people_with_live_items"] = len({
        p for r in results if r.get("status") == "complete"
        for p, n in (r.get("items_by_person") or {}).items() if n})
    out["blocked_roles"] = [{"role": r.get("role"), "status": r.get("status"),
                             "reason": r.get("reason", "")} for r in results
                            if r.get("status") not in ("complete", "no_members")]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_nightly")
    p.add_argument("--store", required=True)
    p.add_argument("--source", default=None)
    p.add_argument("--day", default=None, help="a business day (default: yesterday, US Eastern)")
    p.add_argument("--state-dir", default=os.environ.get("PLX_STATE_DIR", "/data/plx-state"))
    p.add_argument("--roles-file", default=None)
    p.add_argument("--person", action="append", default=[],
                   help="an address scored as its own role (repeatable)")
    p.add_argument("--out-dir", default=None)
    a = p.parse_args(argv)
    state = Path(a.state_dir)
    day = date.fromisoformat(a.day) if a.day else (datetime.now(dc.DAY_TZ) - timedelta(days=1)).date()
    out = run(a.store, a.source, day, state, Path(a.roles_file) if a.roles_file else state / ROLES_DEFAULT,
              a.person)
    dest = Path(a.out_dir or state / "assist-scoreboard")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{day.isoformat()}.json").write_text(json.dumps(out, indent=1, sort_keys=True))
    print(json.dumps({k: out.get(k) for k in ("day", "totals", "roles", "people_with_live_items",
                                               "blocked_roles", "skipped", "status", "reason")}))
    return 1 if out.get("status") == "blocked" or out.get("blocked_roles") or out.get("skipped") else 0


if __name__ == "__main__":
    sys.exit(main())
