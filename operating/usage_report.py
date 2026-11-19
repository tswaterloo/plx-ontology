"""Tokens and API-equivalent cost of every metered Claude Code call, by source and hour.

Reads the files ``operating/usage_meter.py`` writes (``PLX_USAGE_DIR``, default
``/data/plx-state/usage``) and sums the ``api_request`` events: requests, input, output,
cache-read and cache-write tokens and ``cost_usd`` (the API price of the call, what the
plan's limit is measured against), grouped by hour (US Eastern) and by source
(``plx.source``: the systemd unit, ``spark-login`` on the Sparks), host, model, or the module that started the call.

``python3 -m operating.usage_report [--since 6h] [--by source,host,model] [--hourly]``
``python3 -m operating.usage_report --line``: one line for the hourly check.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Iterator, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_DIR = "/data/plx-state/usage"
FIELDS = ("requests", "input", "output", "cache_read", "cache_write", "cost")
KEYS = {
    "source": lambda e: e["res"].get("plx.source") or e["res"].get("service.name") or "?",
    "host": lambda e: e["res"].get("plx.host") or e["res"].get("host.name") or "?",
    "model": lambda e: e["attrs"].get("model") or "?",
    "caller": lambda e: e["res"].get("plx.caller") or "-",
    "query_source": lambda e: e["attrs"].get("query_source") or "-",
    "session": lambda e: e["attrs"].get("session.id") or "-",
}


def since(text: str, now: Optional[dt.datetime] = None) -> dt.datetime:
    """``6h`` / ``30m`` / ``2d`` before now (UTC)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhd])\s*", text or "")
    if not m:
        raise ValueError(f"--since takes 30m, 6h or 2d, got {text!r}")
    unit = {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(
        seconds=float(m.group(1)) * unit)


def when(line: dict[str, Any]) -> Optional[dt.datetime]:
    for text in (line.get("ts"), line.get("rx")):
        if text:
            try:
                return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def read(directory: str, start: dt.datetime,
         end: Optional[dt.datetime] = None) -> Iterator[dict[str, Any]]:
    """Every line received from ``start``'s UTC day on whose time falls in [start, end)."""
    end = end or dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    day = start.date()
    while day <= end.date():
        path = Path(directory) / f"usage-{day:%Y%m%d}.jsonl"
        if path.exists():
            with open(path, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        continue
                    t = when(line)
                    if t is not None and start <= t < end:
                        line["_t"] = t
                        line.setdefault("res", {})
                        line.setdefault("attrs", {})
                        yield line
        day += dt.timedelta(days=1)


def num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def add(row: dict[str, float], attrs: dict[str, Any]) -> None:
    row["requests"] += 1
    row["input"] += num(attrs.get("input_tokens"))
    row["output"] += num(attrs.get("output_tokens"))
    row["cache_read"] += num(attrs.get("cache_read_tokens"))
    row["cache_write"] += num(attrs.get("cache_creation_tokens"))
    row["cost"] += num(attrs.get("cost_usd"))


def summarize(lines: Iterable[dict[str, Any]], by: list[str], hourly: bool) -> dict[str, Any]:
    """{"rows": {(hour?, *keys): totals}, "total": totals, "errors": n, "unparsed": n}."""
    rows: dict[tuple, dict[str, float]] = collections.defaultdict(
        lambda: dict.fromkeys(FIELDS, 0.0))
    total = dict.fromkeys(FIELDS, 0.0)
    errors: collections.Counter = collections.Counter()
    unparsed = 0
    for line in lines:
        if line.get("kind") == "unparsed":
            unparsed += 1
            continue
        if line.get("kind") != "event":
            continue
        if line.get("name") == "api_error":
            errors[KEYS["source"](line)] += 1
            continue
        if line.get("name") != "api_request":
            continue
        key = tuple(KEYS[k](line) for k in by)
        if hourly:
            key = (line["_t"].astimezone(ET).strftime("%m-%d %H:00"),) + key
        add(rows[key], line["attrs"])
        add(total, line["attrs"])
    return {"rows": dict(rows), "total": total, "errors": sum(errors.values()),
            "errors_by_source": dict(errors), "unparsed": unparsed}


def usd(n: float) -> str:
    return f"{n:.2f}" if n >= 1 or n == 0 else f"{n:.6f}"


def tokens(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


def table(summary: dict[str, Any], by: list[str], hourly: bool) -> str:
    head = (["hour ET"] if hourly else []) + by + ["req", "input", "output", "cache_rd",
                                                    "cache_wr", "$ API-eq"]
    out = [head]
    order = sorted(summary["rows"].items(),
                   key=lambda kv: ((kv[0][0],) if hourly else ()) + (-kv[1]["cost"],))
    for key, row in order:
        out.append([str(k) for k in key] + [f"{row['requests']:.0f}", tokens(row["input"]),
                   tokens(row["output"]), tokens(row["cache_read"]),
                   tokens(row["cache_write"]), usd(row["cost"])])
    t = summary["total"]
    out.append(["TOTAL"] + [""] * (len(head) - 7) + [f"{t['requests']:.0f}", tokens(t["input"]),
               tokens(t["output"]), tokens(t["cache_read"]), tokens(t["cache_write"]),
               usd(t["cost"])])
    widths = [max(len(r[i]) for r in out) for i in range(len(head))]
    text = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in out]
    text.insert(1, "  ".join("-" * w for w in widths))
    extra = []
    if summary["errors"]:
        extra.append(f"api errors: {summary['errors']} ("
                     + ", ".join(f"{k} {v}" for k, v in sorted(
                         summary["errors_by_source"].items(), key=lambda kv: -kv[1])) + ")")
    if summary["unparsed"]:
        extra.append(f"unparsed posts: {summary['unparsed']}")
    return "\n".join(text + extra)


def one_line(summary: dict[str, Any], window: str, top: int = 5) -> str:
    t = summary["total"]
    parts = sorted(summary["rows"].items(), key=lambda kv: -kv[1]["cost"])[:top]
    tops = ", ".join(f"{'/'.join(map(str, k))} ${usd(r['cost'])}" for k, r in parts)
    return (f"usage {window}: ${usd(t['cost'])} API-eq, {t['requests']:.0f} req, "
            f"in {tokens(t['input'])} out {tokens(t['output'])} "
            f"cache rd {tokens(t['cache_read'])} wr {tokens(t['cache_write'])}"
            + (f"; top: {tops}" if tops else "")
            + (f"; errors {summary['errors']}" if summary["errors"] else "")
            + (f"; unparsed {summary['unparsed']}" if summary["unparsed"] else ""))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="usage_report", description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="24h", help="window: 30m, 6h, 2d (default 24h)")
    ap.add_argument("--by", default="source,host,model",
                    help="comma list of " + ", ".join(KEYS))
    ap.add_argument("--hourly", action="store_true", help="one row per hour (ET) too")
    ap.add_argument("--line", action="store_true", help="one line for the hourly check")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dir", default=os.environ.get("PLX_USAGE_DIR") or DEFAULT_DIR)
    args = ap.parse_args(argv)
    by = [k.strip() for k in args.by.split(",") if k.strip()]
    bad = [k for k in by if k not in KEYS]
    if bad:
        ap.error(f"--by: unknown {bad}; choose from {list(KEYS)}")
    start = since(args.since)
    if args.line:
        print(one_line(summarize(read(args.dir, start), ["source"], False), args.since))
        return 0
    summary = summarize(read(args.dir, start), by, args.hourly)
    if args.json:
        print(json.dumps({"since": start.isoformat(), "by": by, "total": summary["total"],
                          "errors": summary["errors"], "unparsed": summary["unparsed"],
                          "rows": [{"key": list(k), **v} for k, v in summary["rows"].items()]},
                         indent=1))
        return 0
    print(f"since {start.astimezone(ET):%m-%d %I:%M %p} ET ({args.since})")
    print(table(summary, by, args.hourly))
    return 0


if __name__ == "__main__":
    sys.exit(main())
