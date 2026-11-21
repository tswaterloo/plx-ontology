"""Build, split and refresh the question bank.

    python3 -m evals.qbank.bank generate [--seed S] [--scale X] [--families a,b] [--workers N]
    python3 -m evals.qbank.bank refresh  [--workers N]
    python3 -m evals.qbank.bank stats

Files under ``QBANK_DIR`` (default /data/scratch/qbank, mode 700): ``tune.jsonl``
and ``holdout/holdout.jsonl`` (``runner`` refuses it without
QBANK_HOLDOUT=1). Every item keeps its truth SQL and parameters, so ``refresh`` re-reads the
truth from the records each night; an item whose truth now fails or no longer grades is marked
``stale`` (never sampled) until a later refresh revives it.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import random
import sys
import time
import zoneinfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import db
from .core import Family, instance_id, split_of, stratified, surface

ROOT = Path(os.environ.get("QBANK_DIR", "/data/scratch/qbank"))
TUNE = "tune.jsonl"
HOLDOUT = "holdout/holdout.jsonl"
ET = zoneinfo.ZoneInfo("America/New_York")


def today() -> dt.date:
    return dt.datetime.now(ET).date()


def families() -> list[Family]:
    from . import families_biz, families_ops
    fams = families_biz.FAMILIES + families_ops.FAMILIES
    ids = [f.id for f in fams]
    dup = [i for i, c in collections.Counter(ids).items() if c > 1]
    if dup:
        raise ValueError(f"duplicate family ids: {dup}")
    return fams


def base_params(seed: int, day: dt.date) -> dict[str, Any]:
    return {"seed": str(seed), "seed_n": seed, "pct": 1.0, "today": day.isoformat()}


def instantiate(fam: Family, params: Mapping[str, Any], rng: random.Random, day: dt.date) -> dict[str, Any] | None:
    """One item: phrasing, truth read, grading spec; None when the truth does not grade."""
    shown = {k: v for k, v in params.items() if k not in ("w",)}
    text, variant = surface(fam.asks, shown, rng)
    item = {"id": "", "instance": instance_id(fam, shown), "source": "generated", "family": fam.id, "system": fam.system,
            "type": fam.type, "kind": fam.kind, "variant": variant, "question": text, "turns": text.split(" || "),
            "params": db.jsonable(shown), "truth_sql": fam.truth, "truth_source": fam.truth_source}
    item["id"] = item["instance"]
    return settle(item, fam.grade, day)


def settle(item: dict[str, Any], grade, day: dt.date) -> dict[str, Any] | None:
    bind = dict(item["params"]) | {"today": day.isoformat()}
    t0 = time.monotonic()
    try:
        rows = db.query(item["truth_sql"], bind)
    except db.TruthError as exc:
        if str(exc).startswith("connect:"):
            raise   # no server or no slot: abort the run, never mark items stale for it
        item.update(stale=True, stale_reason=str(exc)[:200], as_of=day.isoformat())
        return item
    try:
        spec = grade(rows, bind)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        spec = None
        item["grade_error"] = f"{type(exc).__name__}: {exc}"[:200]
    item.update(truth_rows=db.jsonable(rows[:40]), truth_row_count=len(rows), truth_ms=int((time.monotonic() - t0) * 1000),
                as_of=day.isoformat())
    if spec is None:
        item.update(stale=True, stale_reason="truth does not grade (empty or ambiguous)")
        return item
    item.update(grading=db.jsonable(spec), stale=False)
    item.pop("stale_reason", None)
    return item


def generate(seed: int, scale: float, only: set[str] | None, workers: int, log=print) -> list[dict[str, Any]]:
    day = today()
    fams = [f for f in families() if not only or f.id in only]
    unknown = (only or set()) - {f.id for f in families()}
    if unknown:
        raise SystemExit(f"unknown families: {sorted(unknown)}")

    def pool(fam: Family) -> tuple[Family, list[dict[str, Any]] | str]:
        try:
            return fam, db.query(fam.pool, base_params(seed, day))
        except db.TruthError as exc:
            return fam, str(exc)

    jobs: list[tuple[Family, dict[str, Any], random.Random]] = []
    with ThreadPoolExecutor(workers) as ex:
        for fam, rows in ex.map(pool, fams):
            if isinstance(rows, str):
                log(f"POOL FAIL {fam.id}: {rows[:200]}")
                continue
            rng = random.Random(f"{seed}/{fam.id}")
            picked = stratified(rows, max(1, round(fam.n * scale)), rng)
            log(f"pool {fam.id}: {len(rows)} candidates, {len(picked)} picked")
            jobs += [(fam, dict(p), random.Random(f"{seed}/{fam.id}/{i}")) for i, p in enumerate(picked)]
    items: list[dict[str, Any]] = []
    errors: collections.Counter = collections.Counter()
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(instantiate, fam, p, rng, day): fam.id for fam, p, rng in jobs}
        for f in as_completed(futs):
            try:
                it = f.result()
            except Exception as exc:  # noqa: BLE001 - a broken family is reported, never half-written
                errors[futs[f]] += 1
                if errors[futs[f]] == 1:
                    log(f"FAMILY ERROR {futs[f]}: {type(exc).__name__}: {exc}")
                continue
            if it:
                it["split"] = split_of(it["instance"])
                items.append(it)
    if errors:
        raise FamilyErrors(dict(errors), items)
    return items


class FamilyErrors(RuntimeError):
    def __init__(self, errors: dict[str, int], items: list[dict[str, Any]]):
        super().__init__(f"families failed: {errors}")
        self.errors, self.items = errors, items


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.open()] if path.exists() else []


def save(items: Iterable[Mapping[str, Any]], root: Path = ROOT) -> dict[str, int]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "holdout").mkdir(mode=0o700, exist_ok=True)
    out = {"tune": [], "holdout": []}
    for it in sorted(items, key=lambda i: i["id"]):
        out[it["split"]].append(it)
    for name, rel in (("tune", TUNE), ("holdout", HOLDOUT)):
        p = root / rel
        tmp = p.with_suffix(".tmp")
        with tmp.open("w") as f:
            for it in out[name]:
                f.write(json.dumps(it, default=str) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    return {k: len(v) for k, v in out.items()}


def all_items(root: Path = ROOT) -> list[dict[str, Any]]:
    return load(root / TUNE) + load(root / HOLDOUT)


def merge(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New items replace old ones of the same id; others stay (a re-run never drops items)."""
    by = {i["id"]: i for i in old}
    by.update({i["id"]: i for i in new})
    return list(by.values())


def refresh(workers: int, root: Path = ROOT, log=print) -> collections.Counter:
    """Re-read every item's truth as of today; returns counts of fresh / stale / revived."""
    from .mine import regrade as mined_grade
    day = today()
    fams = {f.id: f for f in families()}
    items = all_items(root)
    counts: collections.Counter = collections.Counter()

    def one(it: dict[str, Any]) -> dict[str, Any]:
        was = it.get("stale")
        fam = fams.get(it["family"]) if it["source"] == "generated" else None
        if fam is not None:
            it["truth_sql"] = fam.truth   # the family's current definition of the truth
        grade = fam.grade if fam is not None else (mined_grade(it) if it["source"] == "mined" else None)
        if grade is None:
            it.update(stale=True, stale_reason=f"family {it['family']} no longer exists")
        else:
            settle(it, grade, day)
        counts["stale" if it.get("stale") else ("revived" if was else "fresh")] += 1
        return it

    with ThreadPoolExecutor(workers) as ex:
        items = list(ex.map(one, items))
    save(items, root)
    log(json.dumps({"refreshed": len(items), "as_of": day.isoformat(), **counts}))
    return counts


def stats(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    live = [i for i in items if not i.get("stale")]
    by = lambda k: dict(collections.Counter(i[k] for i in live).most_common())
    return {"items": len(items), "live": len(live), "stale": len(items) - len(live),
            "generated": sum(i["source"] == "generated" for i in live), "mined": sum(i["source"] == "mined" for i in live),
            "holdout": sum(i["split"] == "holdout" for i in live), "by_system": by("system"), "by_kind": by("kind"),
            "by_variant": by("variant"), "families": len({i["family"] for i in live}),
            "types": len({i["type"] for i in live})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.qbank.bank")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=1118)
    g.add_argument("--scale", type=float, default=1.0)
    g.add_argument("--families", default="")
    g.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 2) - 2))
    g.add_argument("--dry", action="store_true", help="print a summary, write nothing")
    r = sub.add_parser("refresh")
    r.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 2) - 2))
    sub.add_parser("stats")
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        only = {x for x in a.families.split(",") if x} or None
        try:
            items, failed = generate(a.seed, a.scale, only, a.workers), {}
        except FamilyErrors as exc:
            items, failed = exc.items, exc.errors
        live = [i for i in items if not i.get("stale")]
        print(json.dumps({"generated": len(items), "live": len(live),
                          "stale_by_family": dict(collections.Counter(i["family"] for i in items if i.get("stale")))}))
        if not a.dry:
            print(json.dumps(save(merge(all_items(), live))))
        if failed:
            print(json.dumps({"family_errors": failed}))
            return 1
    elif a.cmd == "refresh":
        refresh(a.workers)
    else:
        print(json.dumps(stats(all_items()), indent=1))
    return 0


if __name__ == "__main__":
    from scripts.eval_guard import enforce
    enforce()
    sys.exit(main())
