"""Measure when the Ask asks back (``operating.clarify``): a set of clear and genuinely
ambiguous questions, each read by the real front door and, when it flags an open choice,
decided by the real clarifier (the operator's Claude login, warm sessions).

``build``: from the held-out Ask sets (``--source`` jsonl files of ``{"question",
"start_type", "role"}`` items), with a fixed seed:

* clear: questions as written (each names its record), offered the one record it names;
* ambiguous: the same kind of question with the record number taken out, so the record
  is not stated and nothing in the question infers it (a question still naming it by a
  title or name is not used), offered that record and others of its type from the set
  (real records of the evaluation truth).

Each class is split dev / held; only dev may be read while the rules are worked on.

``run``: each item's decision (``answer``, ``assume``, ``ask``) with its options; ``score``:
the ask rate on clear items (target under 3%) and the ask-or-assume rate on ambiguous
ones (target at least 90%), with the split by kind.

Evidence label: the decision is live model output on held-out questions; the candidates
are the evaluation truth's records (the reader's own ``gather`` is covered in the tests).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import re
import sys
import time
from typing import Any

_ID = re.compile(r"(?<![\w-])(?=[\w-]*\d)[A-Za-z]*[\w./-]*\d[\w./-]*(?![\w-])")
_DATEISH = re.compile(r"^\d{4}$|^\d{1,2}(?:st|nd|rd|th)?$|^\d{4}-\d{2}-\d{2}$")


# a question about one record (an aggregate over filters names no record to take out)
_AGGREGATE = re.compile(r"\b(?:how many|count|matching|total|lowest|highest|average)\b",
                        re.I)


# what still names the record once its number is out (a quoted title, a product's
# capitalised name): such a question is not ambiguous, so it is never derived
_NAMED = re.compile(r"['\"“”‘’]|\b[A-Z][a-z]+\s*[/&-]?\s+[A-Z][a-z]+|\.[a-z]{3,4}\b"
                    r"|\b[A-Z]{3,}\s+[A-Z]{3,}|\d{4}-\d{2}-\d{2}|_")


def _ids(question: str) -> list[str]:
    """Record numbers in the question: a code with a letter, or five digits or more;
    never a date, a year or a decimal figure."""
    found = [t.rstrip(".,/") for t in _ID.findall(question)]
    return [t for t in found if not _DATEISH.match(t) and not re.fullmatch(r"\d+\.\d+", t)
            and (re.search(r"[A-Za-z]", t) or len(t) >= 5)
            and len(re.sub(r"\D", "", t)) >= 3]


def build(sources: list[Path], out: Path, per_class: int, seed: int) -> dict[str, int]:
    rows = []
    for path in sources:
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if isinstance(r.get("question"), str) and r.get("start_type")
            and len(_ids(r["question"])) == 1 and not _AGGREGATE.search(r["question"])]
    rng = random.Random(seed)
    rng.shuffle(rows)
    by_type: dict[str, list[str]] = {}
    for r in rows:
        by_type.setdefault(r["start_type"], []).append(_ids(r["question"])[0])
    clear, ambiguous = [], []
    for r in rows:
        ident = _ids(r["question"])[0]
        kind = r["start_type"]
        others = [x for x in dict.fromkeys(by_type[kind]) if x != ident][:3]
        own = {"key": "C1", "type": kind, "label": f"{kind} {ident}", "facts": "",
               "object_id": f"{kind}:{ident}"}
        if len(clear) < per_class:
            clear.append({"id": r.get("id"), "label": "clear", "question": r["question"],
                          "role": r.get("role"), "candidates": [own]})
        elif len(ambiguous) < per_class and others and not _NAMED.search(
                r["question"].replace(ident, " ")[1:]):
            q = re.sub(r"\s+", " ", r["question"].replace(ident, " ")).strip()
            q = re.sub(r"\s+([?.,])", r"\1", q)
            cands = [own] + [{"key": f"C{i + 2}", "type": kind, "label": f"{kind} {x}",
                              "facts": "", "object_id": f"{kind}:{x}"}
                             for i, x in enumerate(others)]
            rng.shuffle(cands)
            for i, c in enumerate(cands):
                c["key"] = f"C{i + 1}"
            ambiguous.append({"id": r.get("id"), "label": "ambiguous", "question": q,
                              "role": r.get("role"), "candidates": cands})
        if len(clear) >= per_class and len(ambiguous) >= per_class:
            break
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for split in ("dev", "held"):
        items = [x for group in (clear, ambiguous) for i, x in enumerate(group)
                 if (i % 6 == 0) == (split == "dev")]
        (out / f"{split}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in items))
        counts[split] = len(items)
    return counts


def run(items: list[dict[str, Any]], out: Path, concurrency: int) -> None:
    from operating.clarify import Clarifier, read_decision
    from operating.front_door import FrontDoor
    # a loaded machine answers slower than the app's budget: an unread item is no decision
    door = FrontDoor(pool=concurrency, seconds=45)
    clarifier = Clarifier(pool=concurrency, seconds=45)
    door.warm()
    clarifier.warm()
    # every session primed before the first item, so none is unread for a cold start
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and (door._free.qsize() < concurrency
                                           or clarifier._free.qsize() < concurrency):
        time.sleep(1)
    done = {json.loads(l)["id"] for l in out.read_text().splitlines()} if out.exists() else set()

    def one(item: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        reading = door.read(item["question"])
        decision, said = None, None
        if reading is not None and reading.open is not None:
            said = clarifier.reply({
                "question": item["question"], "conversation": [],
                "reader": {"name": "an employee", "roles": [item["role"]] if item.get(
                    "role") else []}, "page": None, "open": reading.open,
                "candidates": [{k: c[k] for k in ("key", "type", "label", "facts")}
                               for c in item["candidates"]]})
            decision = read_decision(said, item["candidates"], item["question"],
                                     reading.open.get("among"))
        return {"id": item["id"], "label": item["label"], "question": item["question"],
                "read": reading is not None, "open": reading.open if reading else None,
                "kind": decision.kind if decision else "answer",
                "said": said, "seconds": round(time.monotonic() - started, 1),
                "options": [o.label for o in decision.options] if decision else [],
                "assumption": decision.assumption if decision else ""}
    todo = [x for x in items if x["id"] not in done]
    with ThreadPoolExecutor(concurrency) as pool, out.open("a") as sink:
        for got in pool.map(one, todo):
            sink.write(json.dumps(got) + "\n")
            sink.flush()


def score(path: Path) -> dict[str, Any]:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    out: dict[str, Any] = {"unread": sum(not r["read"] for r in rows)}
    for label in ("clear", "ambiguous"):
        got = [r for r in rows if r["label"] == label and r["read"]]
        kinds = {k: sum(r["kind"] == k for r in got) for k in ("answer", "assume", "ask")}
        out[label] = {"n": len(got), **kinds,
                      "asked_rate": round(kinds["ask"] / len(got), 3) if got else None,
                      "ask_or_assume_rate": round((kinds["ask"] + kinds["assume"])
                                                  / len(got), 3) if got else None}
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--source", type=Path, action="append", required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--per-class", type=int, default=120)
    b.add_argument("--seed", type=int, default=1113)
    r = sub.add_parser("run")
    r.add_argument("--set", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--concurrency", type=int, default=2)
    s = sub.add_parser("score")
    s.add_argument("--results", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "build":
        print(json.dumps(build(args.source, args.out, args.per_class, args.seed)))
    elif args.cmd == "run":
        items = [json.loads(l) for l in args.set.read_text().splitlines() if l.strip()]
        run(items, args.out, args.concurrency)
    else:
        print(json.dumps(score(args.results), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
