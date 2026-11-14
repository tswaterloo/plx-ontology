"""How long a new Teams message takes to reach the autopilot, per stage (aggregates only).

    python3 scripts/autopilot_latency.py [--since 2026-11-13T08:30] [--state /data/plx-state]

Of every Teams message created and first captured since ``--since``: capture (sent -> first
captured version), delta (captured -> the delta layer on disk holding it was written),
pg_apply (layer written -> Postgres applied it), follow (applied -> the autopilot's event),
and end to end (sent -> event, sent -> first prepared item). The event is the first one either
feed stored (``follow`` or the fast feed, ``operating.autopilot_fast``). Reads the capture
store, the delta layers, Postgres's load state and the autopilot queue, all read-only; prints
counts and minutes only, never an id, address or message.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import json
import os
from pathlib import Path
import sqlite3


def _at(text: str) -> datetime:
    text = text.rstrip("Z")
    if "+" in text[10:]:
        text = text[:text.rindex("+")]
    return datetime.fromisoformat(text[:26]).replace(tzinfo=timezone.utc)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _key(object_id: str) -> tuple[str, str] | None:
    try:
        value = json.loads(object_id.split(":", 2)[2])
    except (IndexError, ValueError):
        return None
    return (value.get("chat_id") or value.get("channel_id"), str(value.get("id")))


def measure(state: Path, since: str) -> dict[str, list[float]]:
    captured: dict[tuple[str, str], tuple[datetime, str]] = {}
    with _ro(state / "teams-capture" / "capture.db") as conn:
        for recorded, payload in conn.execute(
                "SELECT recorded_at, payload FROM versions WHERE kind IN ('teams_chat', "
                "'teams_channel') AND recorded_at >= ? AND path NOT LIKE '%/conversation'",
                (since,)):
            row = json.loads(payload).get("row") or {}
            if row.get("kind") not in ("chat_message", "channel_message") \
                    or not row.get("created_at"):
                continue
            key = (row.get("chat_id") or row.get("channel_id"), str(row.get("id")))
            if key not in captured or recorded < captured[key][1]:
                captured[key] = (_at(row["created_at"]), recorded)
    start = _at(since)
    sent = {k: (c, _at(r)) for k, (c, r) in captured.items() if c >= start}
    layers: dict[tuple[str, str], tuple[str, datetime]] = {}
    for path in sorted(glob.glob(str(state / "projection" / "delta" / "delta-*.sqlite"))):
        written = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        with _ro(Path(path)) as conn:
            for (oid,) in conn.execute("SELECT object_id FROM delta_owned WHERE object_id "
                                       "LIKE 'Teams%Message:%'"):
                key = _key(oid)
                if key in sent and key not in layers:
                    layers[key] = (os.path.basename(path), written)
    applied: dict[str, datetime] = {}
    try:
        import psycopg
        with psycopg.connect(os.environ.get("PLX_PG_DSN", "postgresql:///plx")) as pg:
            applied = {name[6:]: at for name, at in pg.execute(
                "SELECT name, updated_at FROM ontology.load_state WHERE name LIKE 'delta:%'")}
    except Exception:  # noqa: BLE001 - without Postgres the apply stage is left out
        pass
    seen: dict[tuple[str, str], datetime] = {}
    item: dict[tuple[str, str], datetime] = {}
    with _ro(state / "autopilot" / "autopilot.sqlite") as conn:
        for oid, at in conn.execute("SELECT object_id, MIN(seen_at) FROM events WHERE "
                                    "object_type LIKE 'Teams%' GROUP BY object_id"):
            if _key(oid) in sent:
                seen[_key(oid)] = _at(at)
        for oid, at in conn.execute(
                "SELECT e.object_id, MIN(i.created_at) FROM items i JOIN events e ON "
                "e.event_id = i.event_id WHERE e.object_type LIKE 'Teams%' GROUP BY e.object_id"):
            if _key(oid) in sent:
                item[_key(oid)] = _at(at)
    out: dict[str, list[float]] = {s: [] for s in ("capture", "delta", "pg_apply", "follow",
                                                   "end_to_end_event", "end_to_end_item")}
    for key, (created, recorded) in sent.items():
        out["capture"].append((recorded - created).total_seconds())
        if key in layers:
            name, written = layers[key]
            out["delta"].append((written - recorded).total_seconds())
            if name in applied:
                out["pg_apply"].append((applied[name] - written).total_seconds())
                if key in seen:
                    out["follow"].append((seen[key] - applied[name]).total_seconds())
        if key in seen:
            out["end_to_end_event"].append((seen[key] - created).total_seconds())
        if key in item:
            out["end_to_end_item"].append((item[key] - created).total_seconds())
    out["_counts"] = [len(sent), len(layers), len(seen), len(item)]
    return out


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autopilot_latency")
    parser.add_argument("--since", required=True, help="ISO time (UTC), e.g. 2026-11-13T08:30")
    parser.add_argument("--state", default="/data/plx-state")
    args = parser.parse_args(argv)
    got = measure(Path(args.state), args.since)
    sent, layered, seen, items = got.pop("_counts")
    print(f"messages sent and captured since {args.since}: {sent}; in a layer on disk: "
          f"{layered}; with an event: {seen}; with an item: {items}")
    for stage, values in got.items():
        if values:
            print(f"{stage:17} n={len(values):5}  p50={_pct(values, 50) / 60:7.1f} min  "
                  f"p95={_pct(values, 95) / 60:7.1f} min")
        else:
            print(f"{stage:17} n=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
