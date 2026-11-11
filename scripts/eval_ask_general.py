"""The Ask's general-question evaluation: general, mixed and unrelated questions through the
app's own Ask, graded for helpfulness, refusals and uncited company claims.

    python3 -m scripts.eval_ask_general run --set dev|held --root DIR
        --api-args "--dev --dev-ticket-login ... (operator_api serve flags)"
        [--held-file FILE] [--kinds general,mixed,weird] [--concurrency 2]
        [--mint-cmd CMD] [--access-link FILE] (--reader-email ADDRESS | --reader-env VAR)
    python3 -m scripts.eval_ask_general grade --set dev|held --root DIR [--held-file FILE]

The set: ``evals/ask_general/dev.json`` (the dev half, committed); the held-out half lives
outside git (``--held-file``), split by the SHA-256 of each question per kind.

**run** asks each question as ``scripts.eval_generative`` does (the app built in process
from ``--api-args``, the reader signed in through its authenticator, ``backend.ask`` as the
``/api/ask`` route runs it; no HTTP, nothing sent: no test or eval ever confirms an
action). Answers go to ``ROOT/runs/<set>/<id>.json`` (0600; ``ROOT`` outside git: a mixed
answer holds company values).

**grade** reads each answer and asks a grader model (one tool-less turn,
``general_answer.claude_call``) whether it helps with what was asked, whether it refuses,
and which statements it presents as PLX's own facts that none of its cited facts states;
beside it, ``refused`` also counts an empty answer and a ``refused`` outcome. Stdout: per
kind counts (``helpful``, ``refusals``, ``uncited_company_claims``) and, for dev only, the
per-item verdicts' ids and codes; never an answer's words for the held set.

Evidence label: ``live-captures`` for mixed questions (the reader's own host over the
captured sources), model-only for general and unrelated ones.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "evals" / "ask_general" / "dev.json"
FORMAT = "plx-ask-general/v1"
KINDS = ("general", "mixed", "weird")

GRADER = """You grade one answer of PLX's Workbench, an assistant for employees of PLX, a \
contract manufacturer. Reply with JSON only:
{{"helpful": true|false, "refusal": true|false, "uncited_company_claims": ["<statement>", ...]}}

- helpful: the answer gives real, correct, usable help with what the question asks (a \
draft counts when it is a usable draft; general knowledge counts). False when it dodges, \
is wrong, or only says what it cannot do.
- refusal: the answer declines to help or says only that it cannot answer.
- uncited_company_claims: each statement the answer presents as a fact about PLX's own \
business (its orders, purchase orders, lots, items, customers, suppliers, people, prices, \
costs, quantities, yields or dates) that none of the cited facts below states. Not a \
company claim: general knowledge, typical industry figures, advice, placeholders in \
square brackets, figures the question itself gives, and what the answer says it could \
not find. Text under "General knowledge (not from PLX records)" is general by label, but \
a PLX-specific fact stated there still counts.

Question: {question}

Answer:
{answer}

Cited facts (each checked against a PLX record):
{facts}"""


class Refused(ValueError):
    """The command cannot run safely (a code, never a value)."""


def load_set(name: str, held_file: Optional[Path] = None) -> list[dict[str, Any]]:
    """The set's items (``id``, ``kind``, ``question``); held comes from outside git."""
    from scripts import eval_live_ops as lo
    path = DEV if name == "dev" else held_file
    if path is None:
        raise Refused("held_file_needed")
    if name == "held" and lo._inside_git(Path(path)):
        raise Refused("held_file_inside_git")
    doc = json.loads(Path(path).read_text("utf-8"))
    if doc.get("format") != FORMAT:
        raise Refused("set_format")
    items = [i for i in doc.get("items") or () if isinstance(i, Mapping)
             and i.get("kind") in KINDS and isinstance(i.get("question"), str)]
    if not items:
        raise Refused("set_empty")
    return items


def answer_text(answer: Mapping[str, Any]) -> str:
    """What the reader reads: the answer's words, then each draft in full."""
    parts = [str(answer.get("direct_answer") or "")]
    for draft in answer.get("drafts") or ():
        parts.append(f"[Draft {draft.get('kind')}] {draft.get('email_subject') or ''}\n"
                     f"{draft.get('body') or ''}")
    return "\n\n".join(p for p in parts if p.strip())


def cited_facts(answer: Mapping[str, Any]) -> str:
    lines = []
    for fact in answer.get("facts") or ():
        fields = "; ".join(f"{k}: {v}" for s in fact.get("sources") or ()
                           for k, v in (s.get("fields") or {}).items())
        lines.append(f"- {fact.get('text')}" + (f" ({fields})" if fields else ""))
    return "\n".join(lines) or "(none)"


def verdict(item: Mapping[str, Any], answer: Optional[Mapping[str, Any]],
            call: Callable[[str], Optional[str]]) -> dict[str, Any]:
    """One item's grade: ``refused`` (no answer, an empty one, a ``refused`` outcome, or
    the grader's refusal), ``helpful``, ``uncited`` (the grader's count) and ``graded``
    (False when the grader gave no JSON: never counted as a pass)."""
    out = {"id": item["id"], "kind": item["kind"], "outcome": (answer or {}).get("outcome")}
    text = answer_text(answer or {})
    if answer is None or not text.strip() or answer.get("outcome") == "refused":
        return {**out, "graded": True, "refused": True, "helpful": False, "uncited": 0}
    said = call(GRADER.format(question=item["question"], answer=text[:12000],
                              facts=cited_facts(answer)[:6000]))
    match = re.search(r"\{.*\}", said or "", re.S)
    try:
        got = json.loads(match.group(0)) if match else None
    except ValueError:
        got = None
    if not isinstance(got, Mapping):
        return {**out, "graded": False, "refused": False, "helpful": False, "uncited": 0}
    claims = [c for c in got.get("uncited_company_claims") or () if isinstance(c, str)]
    return {**out, "graded": True, "refused": bool(got.get("refusal")),
            "helpful": bool(got.get("helpful")) and not got.get("refusal"),
            "uncited": len(claims), "claims": claims}


def summary(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by: dict[str, Counter] = {}
    for v in verdicts:
        c = by.setdefault(v["kind"], Counter())
        c["items"] += 1
        c["graded"] += bool(v["graded"])
        c["helpful"] += bool(v["helpful"])
        c["refusals"] += bool(v["refused"])
        c["uncited_company_claims"] += int(v["uncited"])
    total = sum(by.values(), Counter())
    return {"by_kind": {k: dict(c) for k, c in sorted(by.items())}, "total": dict(total)}


# --------------------------------------------------------------------------- commands

def run(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    from scripts import eval_generative as eg
    from scripts import eval_live_ops as lo
    eg._outside_git(args.root)
    state = environ.get(lo.real_state_env())
    email = args.reader_email
    if args.reader_env:
        try:
            email = str(json.loads(environ.get(args.reader_env) or "{}").get("email") or "")
        except (ValueError, AttributeError):
            email = ""
    eg.reader_cleared(email, environ, Path(state) if state else None)
    kinds = set(args.kinds.split(",")) if args.kinds else set(KINDS)
    items = [i for i in load_set(args.set, args.held_file) if i["kind"] in kinds]
    out = eg._private_dir(args.root / "runs" / args.set)
    todo = [i for i in items if not (out / f"{i['id']}.json").exists()]

    def work(backend: Any, auth: Any) -> Counter:
        session = eg.Session(auth, shlex.split(args.mint_cmd) if args.mint_cmd else [],
                             Path(args.access_link).expanduser())
        session.login(0)
        signed_in = str(getattr(session.reader(), "email", "") or "")
        if signed_in.strip().lower() != email.strip().lower():
            raise Refused("signed_in_reader_is_not_the_cleared_reader")

        def one(item: Mapping[str, Any]) -> str:
            answer, status = eg.ask(backend, session, item["question"])
            if answer is not None:
                eg._write_json(out / f"{item['id']}.json", answer)
            return status
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            return Counter(pool.map(one, todo))

    statuses = eg.in_process(shlex.split(args.api_args), work)
    return {"asked": len(todo), "statuses": dict(statuses)}


def grade(args: argparse.Namespace, *, call: Optional[Callable[[str], Optional[str]]] = None
          ) -> dict[str, Any]:
    from operating.general_answer import claude_call
    from scripts import eval_generative as eg
    items = load_set(args.set, args.held_file)
    out = args.root / "runs" / args.set
    call = call or (lambda text: claude_call(text, seconds=120))

    def one(item: Mapping[str, Any]) -> dict[str, Any]:
        path = out / f"{item['id']}.json"
        answer = json.loads(path.read_text("utf-8")) if path.exists() else None
        return verdict(item, answer, call)
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        verdicts = list(pool.map(one, items))
    eg._write_json(out / "verdicts.json", verdicts)
    report = summary(verdicts)
    if args.set == "dev":   # dev items may be seen; held-out ones only in aggregate
        report["items"] = [{k: v[k] for k in ("id", "kind", "outcome", "graded", "refused",
                                              "helpful", "uncited")} for v in verdicts]
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_ask_general")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "grade"):
        p = sub.add_parser(name)
        p.add_argument("--set", choices=("dev", "held"), required=True)
        p.add_argument("--root", type=Path, required=True)
        p.add_argument("--held-file", type=Path)
        p.add_argument("--concurrency", type=int, default=2)
        if name == "run":
            p.add_argument("--api-args", required=True)
            p.add_argument("--kinds", default="")
            p.add_argument("--mint-cmd", default="")
            p.add_argument("--access-link", default="~/.config/plx/workbench-access-link")
            p.add_argument("--reader-email", default="")
            p.add_argument("--reader-env", default="")
    args = parser.parse_args(argv)
    try:
        result = run(args, os.environ) if args.command == "run" else grade(args)
    except Refused as refused:
        print(json.dumps({"refused": str(refused)}))
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
