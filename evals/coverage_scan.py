"""PB7 coverage scan (Ask plan section 9), model-free: over a bench root's stored answers
(``runs/held/<id>.json``, the app's answer payload), every answer carries ``coverage`` (the
question's parts and what read each, A3b-4); each part with no read is listed so
(reads 0: the app shows it "not read"). ``--words``: the answers' ``text`` holding the
'(unverified)' tag or a system-of-record verdict, the PB7 tags-and-verdicts check.

    python3 evals/coverage_scan.py ROOT [ROOT ...] [--words]

Prints one line per answer that fails and a total line "n of m with coverage" (and
"k answers with tags or verdict words"); exit 1 when any answer fails.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# the PB7 check's own phrases (section 9): what the answer text must never say
VERDICT = re.compile(r"\(unverified\)|system of record|pilot data|still a pilot", re.I)


def scan(roots: list[str], words: bool = False) -> tuple[list[str], int, int, int]:
    """(failure lines, answers, answers with coverage, answers with tag or verdict words)."""
    failures: list[str] = []
    n = covered = worded = 0
    for root in roots:
        for path in sorted(Path(root, "runs", "held").glob("*.json")):
            n += 1
            d = json.loads(path.read_text())
            parts = d.get("coverage")
            if not isinstance(parts, list):
                failures.append(f"{root} {path.stem}: no coverage in the payload")
            else:
                covered += 1
                bad = [p for p in parts if not isinstance(p, dict) or "reads" not in p]
                if bad:
                    failures.append(f"{root} {path.stem}: {len(bad)} parts without a read count")
            if words and VERDICT.search(str(d.get("text") or "")):
                worded += 1
                failures.append(f"{root} {path.stem}: tag or verdict words in the text")
    return failures, n, covered, worded


def main(argv: list[str]) -> int:
    words = "--words" in argv
    roots = [a for a in argv if a != "--words"]
    if not roots:
        print(__doc__)
        return 2
    failures, n, covered, worded = scan(roots, words)
    for line in failures:
        print(line)
    print(f"{covered} of {n} with coverage" + (f"; {worded} answers with tags or verdict "
                                               "words" if words else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
