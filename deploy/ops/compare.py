"""Tally a scoreboard run's .score.json outcomes and compare per card with CARD-STATUS.md.
Usage: python3 compare.py <run-name> [<run-name> ...]  (later runs override earlier per card)
"""
import collections, glob, json, re, sys

S = "/data/scratch"
base = {}
for line in open(f"{S}/ops/handoff/CARD-STATUS.md"):
    m = re.match(r"\| (\S+) \| (\S+) \| (dev-\S+) \| (\S+) \| ?(.*?) ?\|$", line.strip())
    if m:
        base[m.group(3)] = (m.group(1), m.group(2), m.group(4), m.group(5))
now = {}
for run in sys.argv[1:]:
    for f in glob.glob(f"{S}/runs/{run}/work/runs/*/p0-r0.score.json"):
        d = json.load(open(f))
        now[d["item_id"]] = d.get("outcome")
c = collections.Counter(now.values())
print("cards scored:", len(now), dict(c))
moves = collections.Counter()
rows = []
for iid, o in sorted(now.items()):
    b = base.get(iid, ("?", "?", None, ""))
    moves[(b[2], o)] += 1
    if b[2] != o:
        rows.append(f"{b[0]:20} {b[1]:7} {iid:22} {b[2]} -> {o}   (was: {b[3]})")
print("moves (handoff -> now):")
for k, v in sorted(moves.items(), key=lambda x: -x[1]):
    print(f"  {k[0]} -> {k[1]}: {v}")
print("\n".join(rows))
missing = sorted(set(base) - set(now))
print("not yet run:", len(missing))
