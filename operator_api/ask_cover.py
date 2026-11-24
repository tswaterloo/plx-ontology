"""The covering-read pass (Fable spec 16.4 F-R1, mechanism 1): after the session wrote its
answer, one call with no tools reads the answer beside the SCOPE of each read it made (the
host's scope line, the rows counted, a LIMIT reached, the grouping, how many rows each value
of a column has; never the rows) and lists the sentences of three kinds:

A. none / all / only without a read that covers the whole set it speaks of;
B. rows given to a named party that the read neither filters on nor shows per row;
C. a count or total stated without a condition its own read carries.

Each listed sentence goes back to the asking session as "run the read that settles this,
then replace that sentence or say it stands" (``ask_patch``); the host swaps that sentence
only. A flag that proves wrong costs one read and changes nothing. The prompt is Fable's
probe, verbatim (/data/scratch/ask7-1123/finder-probe-p.py): it reads statements and counts,
never words of the question, and names no type of question.

Never silent: a call that fails or whose reply cannot be read is ``not_checked`` with why,
logged, and the answer goes out as written."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from . import ask_patch, ask_refs
from .ask_check import _args, _evidence, _name, model as _model

LOG = logging.getLogger("operator_api.ask")
FEATURE = "ask_cover"
KINDS = ("A", "B", "C")

PROMPT = """You check one answer a company data assistant wrote against a list of the reads it made. You have NO tools. For each read you get its statement or search, how many rows came back, and facts the host computed about it (limit reached, grouped by, how many rows each value of a column has). You do NOT get the rows.
Find only these three kinds of statement in the answer:
A. NONE / ALL / ONLY: the answer says something does not exist, was not found, or that all, every, only, none or both of some set are one way. It is BACKED only if a read's statement covers that whole set (its WHERE is no narrower than the claim, it did not stop at its own LIMIT) and its row count or value counts show it. Otherwise it is unbacked.
B. PARTY: the answer says rows (orders, invoices, items, messages) belong to a named customer or supplier. It is BACKED only if the read those rows came from filters on that party or shows the party per row. A read that selects by item, description or number alone, with no party filter and no party column, does not back it.
C. COUNT WITH A HIDDEN FILTER: the answer states a count or total as if of a whole set, and the read it comes from has an extra condition the sentence does not mention (an exclusion like <> or NOT, a date bound, a status), or another read in the list over the same set without that condition holds a different number.
Ignore everything else (wording, other figures, message content). Reply with ONLY a JSON object: {"flags": [{"kind": "A|B|C", "sentence": "<the answer's sentence, exact>", "why": "<the read number and what it lacks, short>", "would_settle": "<the one read that would settle it>"}]}. An empty list is a good result."""


@dataclass
class Cover:
    status: str                       # checked | not_checked
    flags: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    why: Optional[str] = None

    def line(self) -> str:
        """The one log line: counts and kinds only, never a sentence's words."""
        kinds: dict[str, int] = {}
        for f in self.flags:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
        return json.dumps({"ask_cover": self.status, "flags": len(self.flags), "kinds": kinds,
                           "seconds": round(self.seconds, 1), "why": self.why})


def _values(profile: Mapping[str, Any]) -> str:
    out = []
    for col in profile.get("columns") or ():
        if isinstance(col, Mapping) and col.get("values"):
            out.append(f"{col.get('column')}: " + ", ".join(
                f"{json.dumps(v.get('cell'), default=str)} x{v.get('n')}"
                for v in col["values"] if isinstance(v, Mapping)))
    return " | ".join(out)


def _plain(value: Any) -> Any:
    return value.get("untrusted_text") if isinstance(value, Mapping) else value


def card(number: int, event: Mapping[str, Any]) -> str:
    """One read as the check sees it: its statement or arguments and the host's facts about
    its result, never the rows."""
    args = _args(event)
    got = ask_refs._parsed(event.get("result"))
    got = got if isinstance(got, Mapping) else {"raw": str(got)}
    name = _name(event)
    if name == "plx.sql":
        prof = got.get("profile") if isinstance(got.get("profile"), Mapping) else {}
        grouped = prof.get("grouped_by")
        return (f"[read {number}] plx.sql {args.get('sql') or json.dumps(args, default=str)}\n"
                f"   scope: {_plain(got.get('scope'))}\n"
                f"   rows: {prof.get('rows', got.get('row_count'))}; columns: {got.get('columns')}; "
                f"limit reached: {prof.get('limit_hit')}; grouped by: {grouped}; "
                f"error: {got.get('error') or got.get('raw')}\n"
                f"   value counts: {_values(prof) or 'none computed'}")
    docs = got.get("documents") or got.get("opened") or []
    return (f"[read {number}] {name} {json.dumps(args, default=str)}\n"
            f"   returned: {len(docs)}; coverage: "
            f"{json.dumps(got.get('coverage') or got.get('completeness'), default=str)}; "
            f"error: {got.get('error')}")


def task(question: str, answer: str, events: Sequence[Mapping[str, Any]]) -> str:
    # a page of a result already listed is that read again (same statement, more rows)
    cards = [card(n, e) for n, e in enumerate(
        (e for e in _evidence(events) if not _args(e).get("page")))]
    return (PROMPT + f"\n\nQUESTION: {question}\n\nANSWER:\n{answer}\n\nREADS:\n"
            + "\n".join(cards))


class Unreadable(ValueError):
    pass


def parse(reply: str) -> list[dict[str, Any]]:
    body = str(reply or "")
    body = body[body.find("{"):body.rfind("}") + 1]
    try:
        got = json.loads(body)
    except ValueError:
        raise Unreadable("the reply is not JSON") from None
    flags = got.get("flags") if isinstance(got, Mapping) else None
    if not isinstance(flags, list):
        raise Unreadable("the reply has no flags list")
    out = []
    for f in flags:
        if not isinstance(f, Mapping) or f.get("kind") not in KINDS \
                or not str(f.get("sentence") or "").strip():
            raise Unreadable("a flag that is not of the kinds asked for")
        out.append({"kind": f["kind"], "sentence": str(f["sentence"]).strip(),
                    "why": str(f.get("why") or "").strip(),
                    "would_settle": str(f.get("would_settle") or "").strip()})
    return out


def call_for(asker: Any) -> Callable[[str], Optional[str]]:
    """The one call, no tools and no time limit (``ask_check.call_for``'s, under its own
    ledger name)."""
    from operating import humanize
    return humanize.rewriter(binary=getattr(asker, "claude_bin", "claude") or "claude",
                             model=_model(), seconds=humanize.NO_LIMIT, feature=FEATURE)


def check(asker: Any, question: str, answer: str, events: Sequence[Mapping[str, Any]], *,
          call: Optional[Callable[[str], Optional[str]]] = None) -> Cover:
    """The flags for ``answer`` (as the session wrote it). Any failure is ``not_checked``
    with why, logged, never raised."""
    started = time.monotonic()
    call = call or getattr(asker, "_cover_call", None)
    why = None
    try:
        call = call or call_for(asker)
        prompt = task(question, answer, events)
        for _ in (1, 2):    # one retry of an unreadable reply
            reply = call(prompt)
            if reply is None:
                why = "no reply"
                break
            try:
                return Cover("checked", parse(reply), time.monotonic() - started)
            except Unreadable:
                why = "could not read its reply"
    except Exception as exc:  # noqa: BLE001 - said as not checked, logged by type
        why = f"could not run ({type(exc).__name__})"
    LOG.warning(json.dumps({"ask_cover_failed": why}))
    return Cover("not_checked", [], time.monotonic() - started, why)


def patches(printed: str, written: str, cover: Cover) -> list[ask_patch.Patch]:
    """One patch per listed sentence the answer holds, the flags of one sentence together.
    The check read the answer as the person reads it (``printed``, references printed); the
    patch is on the text the session wrote (``written``): the same sentence by its place when
    both split into as many sentences. A quoted sentence not found, or text that does not
    line up, is skipped and logged: nothing is swapped on a guess."""
    shown, wrote = ask_patch.spans(printed), ask_patch.spans(written)
    by: dict[int, list[Mapping[str, Any]]] = {}
    for f in cover.flags:
        at = ask_patch.find(printed, f["sentence"])
        if at is None or at not in shown or len(shown) != len(wrote):
            LOG.info(json.dumps({"ask_cover_unplaced": f["kind"]}))
            continue
        by.setdefault(shown.index(at), []).append(f)
    return [ask_patch.Patch(wrote[i][0], wrote[i][1], written[wrote[i][0]:wrote[i][1]],
                            "; ".join(f"{f['why']} (kind {f['kind']})" for f in fs),
                            "; ".join(f["would_settle"] for f in fs if f["would_settle"]))
            for i, fs in sorted(by.items())]


def message(patched: Sequence[ask_patch.Patch]) -> str:
    return ask_patch.message(
        patched, "A check of your answer against the scope of your own reads lists these "
        "sentences: each says something wider than the read behind it covers.",
        "For each one run the read that settles it, then say what the whole set holds. "
        "Give every figure as a reference to the result it comes from.")
