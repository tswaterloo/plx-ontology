"""One adjudication pass over the undecided party pairs (C7, master plan M5 step 4-5).

``biz.party_candidates`` holds the pairs ``parties.build`` could not decide on evidence
(about 2,770 customer and 378 supplier pairs on 11-23). This reads each pair once, with both
rows' facts, through a model, and stores what it said WITH the evidence it used; it merges
nothing by itself:

* ``export`` (a Spark, the standby: read-only): the undecided pairs from ``parties.build``
  (no write), one sheet per row (name, codes, email, phone, contact person, website, chain,
  salesperson, currency, source systems, order and invoice counts and first / last dates),
  the facts the two sheets share computed IN CODE, one JSON line per pair in a file; resumable
  (a pair already in the file is skipped; a run is bounded by ``--seconds`` and says what is
  left);
* ``apply`` (the box): the file's lines stored in ``party_candidates.evidence -> 'model'``,
  set-based, the state left ``undecided``;
* ``merge --sample-n 50 --sample-right N``: refused below 48 of 50; otherwise every eligible
  pair becomes ``merged`` by ``model`` and ``parties.build`` joins them as tier ``model``.

Verdict: ``same`` / ``different`` / ``cannot_tell``; ``used`` names facts from ``FACTS`` only
(a name is not a fact). A pair is ELIGIBLE for a merge only when the model said ``same``, cited
at least one fact, the pair is not two accounts one system keeps apart (different codes in the
same system and none shared) and the text could be sent to a model (``withheld``). A reply that
is missing, unreadable, or cites something outside ``FACTS`` is stored as ``cannot_tell`` with
status ``failed`` / ``invalid`` and its reason: never silently dropped. ``verified_shared``
lists the facts the code itself found equal in both sheets, so the sample can see whether
the model's evidence was one of them.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from operating.pg import parties as P

log = logging.getLogger(__name__)
FEATURE = "party_adjudication"
READ_VER = 1
VERDICTS = ("same", "different", "cannot_tell")
FACTS = ("code", "email", "phone", "contact_person", "website", "chain", "salesperson",
         "currency", "source_systems", "activity")
BATCH = 8                 # pairs per model call (a model window: two sheets are ~250 tokens)
PARALLEL = 4
SECONDS = 240.0           # one model call
# facts that can say two rows are one company: found equal in code (``shared``), or the order
# history reading as one account continuing the other (``activity``, checked by the sample).
# A shared chain is a group of companies, never one company; salesperson, currency and system
# say nothing about identity: the model may mention them, they never make a pair eligible.
STRONG = ("code", "email", "phone", "contact_person", "website", "activity")
SAMPLE_N, SAMPLE_RIGHT = 50, 48     # the plan's bar for applying model merges
MODEL_ENV = "PLX_PARTY_MODEL"

SHEETS = {
    "customer": ("SELECT handle, name, codes, email, phone, contact_person, salesperson, "
                 "currency, chain_code AS chain, source_systems, NULL::text AS website "
                 "FROM biz.customers WHERE handle = ANY(%s)",
                 ("sales_orders", "customer_handle", "order_date"),
                 ("sales_invoices", "customer_handle", "invoice_date")),
    "supplier": ("SELECT handle, name, codes, email, phone, NULL::text AS contact_person, "
                 "NULL::text AS salesperson, currency, NULL::text AS chain, source_systems, "
                 "website FROM biz.suppliers WHERE handle = ANY(%s)",
                 ("purchase_orders", "supplier_handle", "order_date"),
                 ("supplier_invoices", "supplier_handle", "document_date")),
}


def sheets(conn: Any, kind: str, handles: Sequence[str]) -> dict[str, dict[str, Any]]:
    """One sheet per handle, set-based: three statements for any number of handles."""
    sql, orders, invoices = SHEETS[kind]
    cur = conn.execute(sql, (list(handles),))
    cols = [d.name for d in cur.description]
    out = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    for label, (view, key, date) in (("orders", orders), ("invoices", invoices)):
        for h, n, lo, hi in conn.execute(
                f"SELECT {key}, count(DISTINCT handle), min({date}), max({date}) FROM biz.{view} "
                f"WHERE {key} = ANY(%s) GROUP BY 1", (list(handles),)).fetchall():
            if h in out:
                out[h][label] = {"n": n, "first": str(lo) if lo else None,
                                 "last": str(hi) if hi else None}
    for sheet in out.values():
        for label in ("orders", "invoices"):
            sheet.setdefault(label, {"n": 0, "first": None, "last": None})
    return out


def shared(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """The facts both sheets hold and agree on, decided in code (a name is not one)."""
    def eq(k: str, f: Callable[[Any], str] = lambda v: str(v or "").strip().lower()) -> bool:
        return bool(f(a.get(k))) and f(a.get(k)) == f(b.get(k))
    out = []
    if {c.lower() for c in P._codes(a.get("codes"))} & {c.lower() for c in P._codes(b.get("codes"))}:
        out.append("code")
    for k in ("email", "website", "chain", "salesperson", "currency"):
        if eq(k):
            out.append(k)
    if P._digits(a.get("phone")) and P._digits(a.get("phone")) == P._digits(b.get("phone")):
        out.append("phone")
    if eq("contact_person", P.fold):
        out.append("contact_person")
    if set(str(a.get("source_systems") or "").split(",")) & set(
            str(b.get("source_systems") or "").split(",")) - {""}:
        out.append("source_systems")
    return out


def _blocked(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    """Why this pair can never be merged by a model ('' it can): two accounts of one system."""
    system = P._same_system_other_code(a, b)
    return f"two {system} accounts" if system else ""


def withheld(*sheets_: Mapping[str, Any]) -> str:
    """Why a sheet may not reach a model ('' it may): the Freightcom rule while it stands."""
    try:
        from integrations.image_ocr_gpu import FREIGHTCOM
    except ImportError:   # the rule was removed with its module (train B): nothing withheld
        return ""
    return "freightcom" if any(FREIGHTCOM.search(str(s.get("name") or "")) for s in sheets_) else ""


def prompt_of(batch: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "You decide whether two records in a company's customer or supplier list are the SAME "
        "real company. A name is not evidence by itself: two different companies can share a "
        "name, and one company can hold two accounts. For each pair answer same, different or "
        "cannot_tell, and in used list the facts you relied on, ONLY from: "
        + ", ".join(FACTS) + ". Use cannot_tell when the facts do not settle it. Reply with "
        'JSON only: [{"n": 1, "verdict": "same", "used": ["phone"], "reason": "<one line>"}], '
        "one object per pair, n as given.", ""]
    for i, item in enumerate(batch, 1):
        lines.append(f"PAIR {i} ({item['kind']}); facts both rows share (checked by code): "
                     f"{', '.join(item['verified_shared']) or 'none'}")
        for tag in ("a", "b"):
            lines.append(f"  {tag.upper()}: " + json.dumps(item[tag + "_sheet"], default=str,
                                                           ensure_ascii=False))
    return "\n".join(lines)


def reading_of(item: Mapping[str, Any], raw: Any) -> dict[str, Any]:
    """The stored reading of one pair from the model's object (None / bad shape: failed)."""
    base = {"verified_shared": item["verified_shared"], "read_ver": READ_VER}
    if not isinstance(raw, dict) or raw.get("verdict") not in VERDICTS:
        return {**base, "verdict": "cannot_tell", "used": [], "status": "failed",
                "reason": "no usable verdict in the model's reply", "eligible": False,
                "why_not": "unreadable reply"}
    used = raw.get("used") if isinstance(raw.get("used"), list) else []
    bad = [u for u in used if u not in FACTS]
    reading = {**base, "verdict": raw["verdict"], "used": [u for u in used if u in FACTS],
               "reason": str(raw.get("reason") or ""), "status": "ok"}
    why = ""
    if bad:
        reading["status"], why = "invalid", f"cited {bad}, which are not facts"
    elif raw["verdict"] != "same":
        why = f"verdict {raw['verdict']}"
    elif not set(reading["used"]) & set(STRONG):
        why = "same with no identity fact cited (a name, chain, currency or system alone)"
    elif not [u for u in reading["used"] if u == "activity" or u in item["verified_shared"]]:
        why = "the cited facts are not shared by both rows (code found none equal)"
    elif item.get("blocked"):
        why = item["blocked"]
    reading["eligible"], reading["why_not"] = not why, why
    return reading


def parse(text: str, size: int) -> list[Any]:
    """The reply's objects by pair number (None for a pair with no object)."""
    out: list[Any] = [None] * size
    try:
        start, end = text.index("["), text.rindex("]") + 1
        rows = json.loads(text[start:end])
    except ValueError:
        return out
    seen: set[int] = set()
    for r in rows if isinstance(rows, list) else []:
        n = r.get("n") if isinstance(r, dict) else None
        if isinstance(n, int) and 1 <= n <= size and n not in seen:
            seen.add(n)
            out[n - 1] = r
        elif isinstance(n, int) and n in seen:
            out[n - 1] = None   # answered twice: unusable
    return out


def claude_read(model: str, *, seconds: float = SECONDS,
                call: Optional[Callable[..., Any]] = None) -> Callable[[Sequence[Mapping[str, Any]]], Optional[list[Any]]]:
    """One ``claude -p`` turn per batch (no tools, the Workbench's own access-token config,
    prompt masked, every call in the model ledger), as ``assist_readings.claude_read``."""
    from operating import chat_ask_model as cam
    run = call or subprocess.run

    def read(batch: Sequence[Mapping[str, Any]]) -> Optional[list[Any]]:
        from operating import model_ledger
        from operating.agent_environment import agent_environment
        from operating.card_text import mask_served
        exe = os.environ.get(cam.BINARY_ENV) or cam.BINARY
        home = os.environ.get(cam.HOME_ENV) or cam.CLAUDE_HOME
        if not cam.credential_path_safe(exe, home):
            log.error("party adjudication: the model's config is not safe; nothing called")
            return None
        prompt = mask_served(prompt_of(batch))
        argv = [exe, "-p", "--setting-sources", "", "--tools", "", "--no-session-persistence",
                "--max-turns", "1", "--output-format", "json", "--model", model]
        env = dict(agent_environment(os.environ))
        env["HOME"] = home
        env["CLAUDE_CONFIG_DIR"] = os.path.join(home, ".claude")
        started = time.monotonic()
        try:
            done = run(argv, input=prompt, capture_output=True, text=True, timeout=seconds,
                       env=env, start_new_session=True)
            reply = json.loads(done.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            model_ledger.record(FEATURE, prompt=prompt, ok=False,
                                seconds=time.monotonic() - started, reason=type(exc).__name__)
            return None
        good = isinstance(reply, dict) and not reply.get("is_error") \
            and isinstance(reply.get("result"), str)
        turns, tokens = model_ledger.usage_of(reply)
        model_ledger.record(FEATURE, prompt=prompt, ok=good, turns=turns, tokens=tokens,
                            seconds=time.monotonic() - started)
        return parse(reply["result"], len(batch)) if good else None
    return read


def items_of(conn: Any, candidates: Sequence[tuple[str, str, str, dict[str, Any]]]
             ) -> list[dict[str, Any]]:
    """The work items for ``(kind, a, b, evidence)`` pairs: both sheets, the code-verified
    shared facts, and ``blocked`` / ``withheld`` where a model must not decide."""
    out: list[dict[str, Any]] = []
    for kind in SHEETS:
        pairs = [c for c in candidates if c[0] == kind]
        got = sheets(conn, kind, sorted({h for c in pairs for h in c[1:3]})) if pairs else {}
        for _, a, b, ev in pairs:
            if a not in got or b not in got:
                continue
            sa, sb = got[a], got[b]
            out.append({"kind": kind, "a": a, "b": b, "pair_evidence": ev, "a_sheet": sa,
                        "b_sheet": sb, "verified_shared": shared(sa, sb),
                        "blocked": _blocked(sa, sb), "withheld": withheld(sa, sb)})
    return out


def judge(items: Sequence[Mapping[str, Any]], read: Callable[..., Any], model: str, *,
          deadline: float | None = None) -> tuple[list[dict[str, Any]], int]:
    """(one stored line per pair judged, the pairs left when the deadline came). A withheld
    pair is stored as such and never sent; a batch the model did not answer is stored failed."""
    rows: list[dict[str, Any]] = []
    todo: list[Mapping[str, Any]] = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for it in items:
        if it["withheld"]:
            rows.append(_row(it, {"verdict": "cannot_tell", "used": [], "status": "withheld",
                                  "reason": f"not sent to a model: {it['withheld']}",
                                  "eligible": False, "why_not": "withheld",
                                  "verified_shared": it["verified_shared"],
                                  "read_ver": READ_VER}, model, now))
        else:
            todo.append(it)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    left = 0
    with cf.ThreadPoolExecutor(PARALLEL) as pool:
        futures = {}
        for n, batch in enumerate(batches):
            if deadline is not None and time.monotonic() > deadline:
                left += len(batch)
                continue
            futures[pool.submit(read, batch)] = batch
        for fut in cf.as_completed(futures):
            batch = futures[fut]
            got = fut.result()
            for i, it in enumerate(batch):
                raw = got[i] if got is not None and i < len(got) else None
                rows.append(_row(it, reading_of(it, raw), model, now))
    return rows, left


def _row(it: Mapping[str, Any], reading: Mapping[str, Any], model: str, at: str) -> dict[str, Any]:
    return {"kind": it["kind"], "a": it["a"], "b": it["b"], **reading, "model": model,
            "read_at": at}


def export(dsn: str, out: Path, *, seconds: float = 3000.0, limit: int | None = None,
           read: Optional[Callable[..., Any]] = None, model: str = "") -> dict[str, Any]:
    """The undecided pairs read once each into ``out`` (JSON lines; resumable)."""
    import psycopg
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done.add((r["a"], r["b"]))
            except (ValueError, KeyError):
                continue
    if read is None:
        model = model or os.environ.get(MODEL_ENV, "haiku")
        read = claude_read(model)
    built = P.build(dsn, write=False)
    cands = _undecided(built)
    todo = todo_all = [c for c in cands if (c[1], c[2]) not in done]
    if limit:
        todo = todo[:limit]
    with psycopg.connect(dsn) as conn:
        items = items_of(conn, todo)
    rows, left = judge(items, read, model, deadline=time.monotonic() + seconds)
    with out.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    count: dict[str, int] = {}
    for r in rows:
        key = f"{r['status']}:{r['verdict']}"
        count[key] = count.get(key, 0) + 1
    return {"undecided": len(cands), "already_read": len(cands) - len(todo_all),
            "read_now": len(rows), "left": left + len(todo_all) - len(todo) + max(0, len(todo) - len(items)),
            "by_status_verdict": count, "eligible": sum(1 for r in rows if r["eligible"])}


def _undecided(built: Mapping[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
    """``(kind, a, b, evidence)`` of the undecided pairs of a ``parties.build(write=False)``."""
    kind_of_party = {p[0]: p[1] for p in built["_parties"]}
    kind_of = {m[0]: kind_of_party[m[1]] for m in built["_members"]}
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for a, b, ev, state, _who in built["_candidates"]:
        if state == "undecided":   # one pair once, whatever reasons listed it (name, lead link)
            merged.setdefault((a, b), {}).update(json.loads(ev) if isinstance(ev, str) else ev)
    return [(kind_of[a], a, b, ev) for (a, b), ev in sorted(merged.items())]


def apply(conn: Any, path: Path) -> dict[str, Any]:
    """Store the file's readings in ``party_candidates.evidence -> 'model'`` (one statement);
    the state stays ``undecided``."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    payload = [{"a": r["a"], "b": r["b"], "m": {k: v for k, v in r.items()
                                              if k not in ("kind", "a", "b")}} for r in rows]
    got = conn.execute(
        "UPDATE biz.party_candidates c SET evidence = c.evidence || jsonb_build_object('model', v.m) "
        "FROM jsonb_to_recordset(%s::jsonb) AS v(a text, b text, m jsonb) "
        "WHERE c.handle_a = v.a AND c.handle_b = v.b AND c.state = 'undecided'",
        (json.dumps(payload),))
    return {"lines": len(rows), "stored": got.rowcount}


def sample(conn: Any, n: int = SAMPLE_N, seed: int = 1) -> list[dict[str, Any]]:
    """``n`` eligible merges at random with their stored evidence."""
    rows = conn.execute("SELECT handle_a, handle_b, evidence FROM biz.party_candidates "
                        "WHERE state = 'undecided' AND evidence->'model'->>'eligible' = 'true' "
                        "ORDER BY handle_a, handle_b").fetchall()
    random.Random(seed).shuffle(rows)
    return [{"a": a, "b": b, "evidence": ev} for a, b, ev in rows[:n]]


class SampleNotMet(RuntimeError):
    pass


def merge(conn: Any, sample_n: int, sample_right: int) -> dict[str, Any]:
    """Every eligible pair becomes ``merged`` by ``model``; refused unless the sample of
    ``SAMPLE_N`` was checked and ``SAMPLE_RIGHT`` were right."""
    if sample_n < SAMPLE_N or sample_right < SAMPLE_RIGHT:
        raise SampleNotMet(f"the sample must be {SAMPLE_RIGHT} right of {SAMPLE_N}; got "
                           f"{sample_right} of {sample_n}: no merge applied")
    got = conn.execute("UPDATE biz.party_candidates SET state = 'merged', decided_by = 'model' "
                       "WHERE state = 'undecided' AND evidence->'model'->>'eligible' = 'true'")
    return {"merged": got.rowcount, "sample": f"{sample_right} of {sample_n}",
            "next": "python3 -m operating.pg.parties build"}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="party_adjudication")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("--out", required=True)
    e.add_argument("--seconds", type=float, default=3000.0); e.add_argument("--limit", type=int)
    e.add_argument("--dsn")
    for name in ("apply", "sample", "merge"):
        p = sub.add_parser(name); p.add_argument("--dsn")
        if name == "apply":
            p.add_argument("--file", required=True)
        if name == "sample":
            p.add_argument("--n", type=int, default=SAMPLE_N); p.add_argument("--seed", type=int, default=1)
        if name == "merge":
            p.add_argument("--sample-n", type=int, required=True)
            p.add_argument("--sample-right", type=int, required=True)
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.cmd == "export":
        print(json.dumps(export(dsn, Path(args.out), seconds=args.seconds, limit=args.limit)))
        return 0
    import psycopg
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            if args.cmd == "apply":
                print(json.dumps(apply(conn, Path(args.file))))
            elif args.cmd == "sample":
                print(json.dumps(sample(conn, args.n, args.seed), default=str, indent=1))
            else:
                try:
                    print(json.dumps(merge(conn, args.sample_n, args.sample_right)))
                except SampleNotMet as exc:
                    print(json.dumps({"refused": str(exc)}))
                    return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
