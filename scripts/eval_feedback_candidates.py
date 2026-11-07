"""Eval candidates from the app's feedback, made offline, never by the product.

    python3 -m scripts.eval_feedback_candidates --state-dir DIR [--recompute]
        [--held-out-digests FILE ...] [--out FILE]

**What it reads.** The feedback store the API keeps (``<state>/feedback/feedback.sqlite``,
``PLX_FEEDBACK_DIR`` overrides), opened read-only: each answer entry with a question and the
reader's "right answer", and the claims and citations as the reader saw them. The product
(``operator_api.feedback``) stores only that; it never imports eval code.

**What it makes.** One pending candidate per such entry, with a live-ops key
(``evals.live_ops.KEY_FORMAT``) for the right answer read from the cited records (the
flagged claims' when any were flagged): one fact of the kind the value is (a date, a
number, nothing on record, else text), with ``evals.live_ops.key_problems`` to fix. A
question that is a live-ops card's keeps its family and slots; ``--recompute`` computes its
key from the records (``evals.live_ops_keys.FAMILIES`` over
``scripts.eval_live_ops.load_world``).

**Held out stays unseen.** Before anything is written, every candidate whose partition keys
(``evals.partition.key_digests`` of the candidate as an item: its question, its key's
records and listed members) meet a held-out item's is dropped, counted, never shown. The
held-out digests come from the frozen live held-out sets in the state directory
(``scripts.eval_oracle_dev.frozen_heldout_keys``), the live-ops held-out splits
(``$PLX_EVAL_HELD_OUT_DIR/live-ops/sets``, the credentialed host only) and any
``--held-out-digests`` file (``{"key_sha256": [...]}``). When the live-ops held-out splits
cannot be read and no digest file is given, nothing is written (fail closed).

Output: ``eval-candidates.json`` beside the store (or ``--out``), 0600; never part of any
eval set until a person audits each key and adds it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

FORMAT = "plx-feedback-eval-candidates/v1"
_STOP = frozenset("a an the is are was were of for to in on at by and or what whats "
                  "what's how many much do does did we our my me i it its this that "
                  "which who when where why with from any there".split())
_CARDS: Optional[list[tuple[str, str, re.Pattern[str]]]] = None


class Refused(RuntimeError):
    """Nothing is written: the reason is the message."""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[?!.,;:]+$", "", text.strip().lower()))


def _cards() -> list[tuple[str, str, re.Pattern[str]]]:
    """Each live-ops card's question forms as patterns: (card id, family, pattern)."""
    global _CARDS
    if _CARDS is None:
        out = []
        path = Path(__file__).resolve().parent.parent / "evals" / "live_ops" / "cards.json"
        try:
            cards = json.loads(path.read_text("utf-8"))["cards"]
        except (OSError, ValueError, KeyError):
            cards = []
        for card in cards:
            for form in [card.get("question") or "", *(card.get("messy") or [])]:
                slots = re.findall(r"\{(\w+)\}", form)
                if not form or len(slots) != len(set(slots)):
                    continue
                parts = re.split(r"(\{\w+\})", _norm(form))
                pattern = "".join(
                    f"(?P<{p[1:-1]}>[a-z0-9][\\w./#-]*)" if re.fullmatch(r"\{\w+\}", p)
                    else re.escape(p) for p in parts)
                out.append((card["id"], card["family"], re.compile(pattern + r"$")))
        _CARDS = out
    return _CARDS


def match_card(question: str) -> Optional[tuple[str, str, dict[str, str]]]:
    """The live-ops card whose question form the question is, with its slots."""
    asked = _norm(question)
    for card_id, family, pattern in _cards():
        found = pattern.fullmatch(asked)
        if found:
            return card_id, family, {k: v for k, v in found.groupdict().items() if v}
    return None


def _system(handle: str) -> Optional[str]:
    from evals.live_ops import SYSTEMS
    scheme = handle.split("://", 1)[0].lower() if "://" in handle else ""
    return scheme if scheme in SYSTEMS else None


def candidate_key(feedback_id: str, question: str, expected: str,
                  claims: Sequence[Mapping[str, Any]], flagged: Sequence[int]) -> dict[str, Any]:
    """A live-ops key for the person's right answer, read from the records the answer
    cited, with its ``problems``, family and slots (``key_source``: correction)."""
    from evals import live_ops
    from evals import score as scoring
    from evals.live_ops_keys import _fact

    picked = [c for i, c in enumerate(claims) if i in set(flagged)] or list(claims)
    records = sorted({s["handle"] for c in picked for s in c.get("citations") or ()
                      if s.get("handle") and _system(s["handle"])})
    allowed = sorted({_system(h) for h in records} - {None})
    value = expected.strip()
    words = [w for w in re.findall(r"[a-z][a-z'-]+", question.lower()) if w not in _STOP][:3]
    if re.match(r"^(?:no|none|nothing|there (?:is|are) no)\b", value, re.IGNORECASE):
        fact = _fact("absent", "f1", records, allowed, value=value)
    elif live_ops._day(value) is not None:
        fact = _fact("date", "f1", records, allowed, value=str(live_ops._day(value)))
    elif re.fullmatch(r"[$€£]?\s*-?[\d,]+(?:\.\d+)?\s*%?", value) \
            and scoring.to_decimal(re.sub(r"[$€£,%\s]", "", value)) is not None:
        number = str(scoring.to_decimal(re.sub(r"[$€£,%\s]", "", value)))
        fact = _fact("number", "f1", records, allowed, value=number, label=words,
                     unit="%" if value.endswith("%") else None)
    else:
        fact = _fact("text", "f1", records, allowed, value=value)
    key = {"format": live_ops.KEY_FORMAT, "item_id": f"FB-{feedback_id}",
           "query": {"rule": "feedback_correction: the person's right answer, to be checked "
                             "against the cited records", "reads": records},
           "facts": [fact]}
    card = match_card(question)
    return {"key": key, "problems": live_ops.key_problems(key),
            "family": card[1] if card else None, "slots": card[2] if card else {},
            "key_source": "correction"}


def read_feedback(directory: Path) -> list[dict[str, Any]]:
    """Answer entries with a question and a right answer, from the store, read-only."""
    path = directory / "feedback.sqlite"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, at, question, expected, status, context FROM feedback WHERE "
            "kind = 'answer' AND expected IS NOT NULL AND expected != '' AND question "
            "IS NOT NULL AND question != '' AND status != 'wont_fix' ORDER BY at").fetchall()
    finally:
        conn.close()
    return [dict(r, context=json.loads(r["context"] or "{}")) for r in rows]


def build(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for entry in entries:
        ctx = entry.get("context") or {}
        built = candidate_key(entry["id"], entry["question"], entry["expected"],
                              ctx.get("claims") or [], ctx.get("flagged") or [])
        out.append({"id": "cand-" + entry["id"], "feedback_id": entry["id"],
                    "at": entry["at"], "triage": entry["status"],
                    "question": entry["question"], "expected": entry["expected"], **built})
    return out


def recompute(candidates: list[dict[str, Any]], state: Path) -> dict[str, Any]:
    """Each candidate of a live-ops family: its key computed from the records, when the
    family has a record with the question's slots."""
    from evals import live_ops
    from evals import live_ops_keys as keys
    from scripts.eval_live_ops import load_world

    pending = [c for c in candidates if c.get("family")]
    done: dict[str, Any] = {"of_a_family": len(pending), "recomputed": 0, "no_record": 0}
    if not pending:
        return done
    try:
        world, _ = load_world(Path(state))
    except Exception as error:  # noqa: BLE001 - no records to read: the keys stay as made
        return {**done, "world_unreadable": type(error).__name__}
    for cand in pending:
        family = keys.FAMILIES.get(cand["family"])
        slots = {k: str(v).lower() for k, v in (cand.get("slots") or {}).items()}
        found = None
        try:
            for c in family(world) if family else ():
                if slots and all(str(c.slots.get(k, "")).lower() == v for k, v in slots.items()):
                    found = c
                    break
        except Exception:  # noqa: BLE001 - a family that fails leaves the correction key
            found = None
        if found is None:
            done["no_record"] += 1
            continue
        cand["key"] = {"format": live_ops.KEY_FORMAT, "item_id": cand["key"]["item_id"],
                       "query": {"rule": found.rule, "reads": list(found.reads)},
                       "facts": list(found.facts)}
        cand["problems"] = live_ops.key_problems(cand["key"])
        cand["key_source"] = "records"
        done["recomputed"] += 1
    return done


def as_item(cand: Mapping[str, Any]) -> dict[str, Any]:
    """The candidate as ``evals.partition`` reads an item (``eval_live_ops._shadow``)."""
    key = cand["key"]
    cites = [{"l1_handle": h} for f in key.get("facts") or () for h in f.get("records") or ()]
    named = " ".join(form for f in key.get("facts") or () for member in f.get("members") or ()
                     for form in member)
    question = {"text": cand["question"], "paraphrases": [named] if named else []}
    slots = cand.get("slots") or {}
    root = {"identifier": next(iter(slots.values()))} if len(slots) == 1 else {}
    return {"id": cand["id"], "question": question, "root": root,
            "known_outcome": {"values": [{"citations": cites}]} if cites else {}}


def held_out_digests(state: Path, files: Sequence[Path]) -> set[str]:
    """Every held-out item's partition key digests (module doc); Refused when the live-ops
    held-out splits cannot be read and no digest file names them."""
    from evals import partition
    from scripts import eval_live_ops as ops
    from scripts.eval_oracle_dev import frozen_heldout_keys

    digests: set[str] = set()
    for path in files:
        digests |= set(json.loads(Path(path).read_text("utf-8")).get("key_sha256") or ())
    digests |= frozen_heldout_keys(state)[0]
    try:
        from evals import runner
        sets = runner.held_out_root() / ops.HELD_OUT_SUBDIR / "sets"
    except Exception as error:  # noqa: BLE001 - not the credentialed host
        if not files:
            raise Refused(f"held_out_unreadable:{type(error).__name__}") from None
        return digests
    for item_path in sorted(sets.glob("*/items/*.json")):
        key_path = item_path.parent.parent / "keys" / item_path.name
        item = json.loads(item_path.read_text("utf-8"))
        key = json.loads(key_path.read_text("utf-8")) if key_path.exists() else {"facts": []}
        digests |= partition.key_digests(ops._shadow(item, key))
    return digests


def drop_held_out(candidates: list[dict[str, Any]], digests: set[str]
                  ) -> tuple[list[dict[str, Any]], int]:
    from evals import partition
    kept = [c for c in candidates if not (partition.key_digests(as_item(c)) & digests)]
    return kept, len(candidates) - len(kept)


def write(path: Path, body: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def run(state: Path, *, recompute_keys: bool = False, digest_files: Sequence[Path] = (),
        out: Optional[Path] = None, feedback_dir: Optional[Path] = None) -> dict[str, Any]:
    directory = feedback_dir or Path(os.environ.get("PLX_FEEDBACK_DIR")
                                     or Path(state) / "feedback")
    held = held_out_digests(Path(state), digest_files)   # before anything is built
    candidates = build(read_feedback(directory))
    counts: dict[str, Any] = {"entries": len(candidates)}
    if recompute_keys:
        counts.update(recompute(candidates, Path(state)))
    candidates, dropped = drop_held_out(candidates, held)
    counts.update(held_out_dropped=dropped, written=len(candidates))
    path = write(out or directory / "eval-candidates.json", {
        "format": FORMAT, "note": "Pending review: never part of any eval set until a person "
                                  "audits each key and adds it.",
        "counts": counts, "candidates": candidates})
    return {"candidates": str(path), **counts}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_feedback_candidates")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--recompute", action="store_true",
                        help="compute keys from the records for live-ops families")
    parser.add_argument("--held-out-digests", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        done = run(args.state_dir, recompute_keys=args.recompute,
                   digest_files=args.held_out_digests, out=args.out)
    except Refused as why:
        print(json.dumps({"refused": str(why)}), file=sys.stderr)
        return 2
    print(json.dumps(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
