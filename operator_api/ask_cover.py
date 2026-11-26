"""The covering-read pass (Fable spec 16.4 F-R1, mechanism 1): after the session wrote its
answer, one call with no tools reads the answer beside the SCOPE of each read it made (the
host's scope line, the rows counted, a LIMIT reached, the grouping, how many rows each value
of a column has; never the rows) and lists the sentences of three kinds:

A. none / all / only without a read that covers the whole set it speaks of;
B. rows given to a named party that the read neither filters on nor shows per row;
C. a count or total stated without a condition its own read carries.

The listed sentences go back to the asking session as one turn: run the read that settles
each, then write the whole answer again (or say ``stands``). The host never writes into
the answer itself: no sentence of its own, none of the model's reply spliced into the
old text (11-24: a reply that began "stands." was spliced into a bullet). A flag that proves
wrong costs one read and changes nothing. The prompt is Fable's
probe, verbatim (/data/scratch/ask7-1123/finder-probe-p.py): it reads statements and counts,
never words of the question, and names no type of question.

Never silent: a call that fails or whose reply cannot be read is ``not_checked`` with why,
logged, and the answer goes out as written."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from . import ask_patch, ask_refs
from .ask_check import _args, _evidence, _name

LOG = logging.getLogger("operator_api.ask")
FEATURE = "ask_cover"
MODEL_ENV = "PLX_ASK_COVER_MODEL"
MODEL = "haiku"
KINDS = ("A", "B", "C")
EFFORT_ENV = "PLX_ASK_COVER_EFFORT"
EFFORT = "low"
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
SECONDS_ENV = "PLX_ASK_COVER_SECONDS"
SECONDS = 12.0
MODE_ENV = "PLX_ASK_COVER_MODE"
MODE = "smart"
MODES = frozenset({"smart", "always", "background"})
_SQL_MAX_CHARS = 1200
_VALUES_MAX_CHARS = 800

# High-risk universal, absence, ranking, or aggregate phrasing that warrants a synchronous
# covering-read check when paired with filtered, grouped, or truncated reads.
_RISK_CLAIMS = re.compile(
    r"\b(?:none|nothing|never|zero|both|every|all\s+(?:\d|of|the|our|open|pending|late|items|orders|months|rows|records|messages|mail)|"
    r"only\s+(?:\d|one|two|three|four|five|the|a\b)|"
    r"no\s+(?:matching|open|late|other|further|later|record|records|order|orders|invoice|invoices|mail|email|emails|message|messages|meeting|meetings|transcript|transcripts|price|prices|item|items|result|results|data|mention|mentions|reply|replies)|"
    r"not\s+(?:in\s+the\s+system|in\s+our\s+system|found|recorded|billed|paid|shipped|received|mentioned)|"
    r"most\s+(?:overloaded|expensive|active|urgent|recent|late)|"
    r"highest|lowest|largest|smallest|biggest|top\s+\d|total(?:s|ed|ing)?)\b",
    re.I,
)
_SQL_FILTER_OR_AGG = re.compile(
    r"\b(?:where|group\s+by|limit|sum\s*\(|count\s*\(|avg\s*\(|min\s*\(|max\s*\(|having|filter\s*\()\b",
    re.I,
)


def cover_mode(environ: Mapping[str, str] = os.environ) -> str:
    """Execution mode for ``ask_cover``: ``smart`` (default: sync only on high-risk
    universal/absence/aggregate/truncated/untraced answers, background otherwise),
    ``always`` (always sync), or ``background`` (never block the answer)."""
    raw = str(environ.get(MODE_ENV) or "").strip().lower()
    return raw if raw in MODES else MODE


def needs_sync_check(printed: str, events: Sequence[Mapping[str, Any]],
                     untraced: Sequence[str] = ()) -> tuple[bool, str]:
    """Fast deterministic pre-screen deciding whether ``ask_cover`` should run
    synchronously before streaming finishes or asynchronously in the background.

    Sync runs whenever:
    - the answer contains any untraced figures;
    - any SQL or exact search read hit a limit, truncation, or error (excluding fuzzy
      ``meaning_continue`` / ``attachments_continue`` cursors when exact matches are complete);
    - the answer makes universal/absence/ranking/aggregate claims backed by filtered,
      grouped, or aggregated reads.
    """
    if untraced:
        return True, "untraced_figures"
    ev_list = [e for e in _evidence(events) if not _args(e).get("page")]
    if not ev_list:
        return False, "no_reads"
    has_scoped_or_agg = False
    for e in ev_list:
        args = _args(e)
        got = ask_refs._parsed(e.get("result"))
        got = got if isinstance(got, Mapping) else {}
        name = _name(e)
        if e.get("is_error") or got.get("error"):
            return True, "read_error"
        if name == "plx.sql":
            prof = got.get("profile") if isinstance(got.get("profile"), Mapping) else {}
            if prof.get("limit_hit") or got.get("truncated"):
                return True, "sql_limit_or_truncated"
            sql_text = str(args.get("sql") or "")
            if (prof.get("grouped_by") or args.get("measure")
                    or _SQL_FILTER_OR_AGG.search(sql_text)):
                has_scoped_or_agg = True
        elif name == "plx.search":
            comp = got.get("completeness") if isinstance(got.get("completeness"), Mapping) else {}
            reason = str(comp.get("reason") or "")
            widened_only = reason in ("meaning_continue", "attachments_continue")
            if (comp.get("truncated") or comp.get("next_cursor")) and not widened_only:
                return True, "search_truncated"
            if args.get("since") or args.get("until") or args.get("participant") or args.get("sources"):
                has_scoped_or_agg = True
        else:
            has_scoped_or_agg = True
    if has_scoped_or_agg and _RISK_CLAIMS.search(str(printed or "")):
        return True, "scoped_universal_or_aggregate"
    return False, "low_risk_reads_complete"


def as_claims(flags: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert ``ask_cover`` flags into ``ask_check.Verdict`` claim entries so telemetry
    and answer payloads accurately report flagged claim counts."""
    out: list[dict[str, Any]] = []
    for f in flags:
        if not isinstance(f, Mapping):
            continue
        sentence = str(f.get("sentence") or "").strip()
        if not sentence:
            continue
        out.append({
            "claim": sentence,
            "verdict": "needs_cover",
            "cells": str(f.get("why") or "").strip(),
            "read": None,
            "would_settle": str(f.get("would_settle") or "").strip(),
            "kind": str(f.get("kind") or "A"),
        })
    return out


def model(environ: Mapping[str, str] = os.environ) -> str:
    """Model for the covering-read check (default ``haiku`` via ``PLX_ASK_COVER_MODEL``)."""
    return str(environ.get(MODEL_ENV) or "").strip() or MODEL


def cover_effort(environ: Mapping[str, str] = os.environ) -> Optional[str]:
    """Reasoning effort for the covering-read call (default ``low`` for fast JSON triage)."""
    raw = str(environ.get(EFFORT_ENV) or "").strip().lower()
    if raw == "default":
        return None
    return raw if raw in EFFORTS else EFFORT


def cover_seconds(environ: Mapping[str, str] = os.environ) -> float:
    """Timeout in seconds for the covering-read call (default 12 s)."""
    raw = str(environ.get(SECONDS_ENV) or "").strip()
    if not raw:
        return SECONDS
    try:
        val = float(raw)
        return val if val > 0 else SECONDS
    except ValueError:
        return SECONDS

PROMPT = """You check one answer a company data assistant wrote against a list of the reads it made. You have NO tools. For each read you get its statement or search, how many rows came back, and facts the host computed about it (limit reached, grouped by, how many rows each value of a column has). You do NOT get the rows.
Find only these three kinds of statement in the answer:
A. NONE / ALL / ONLY: the answer says something does not exist, was not found, or that all, every, only, none or both of some set are one way. It is BACKED only if a read's statement covers that whole set (its WHERE is no narrower than the claim, it did not stop at its own LIMIT) and its row count or value counts show it. Otherwise it is unbacked.
B. PARTY: the answer says rows (orders, invoices, items, messages) belong to a named customer or supplier. It is BACKED only if the read those rows came from filters on that party or shows the party per row. A read that selects by item, description or number alone, with no party filter and no party column, does not back it.
C. COUNT WITH A HIDDEN FILTER: the answer states a count or total as if of a whole set, and the read it comes from has an extra condition the sentence does not mention (an exclusion like <> or NOT, a date bound, a status), or another read in the list over the same set without that condition holds a different number.
Ignore everything else (wording, other figures, message content). Reply with ONLY a JSON object: {"flags": [{"kind": "A|B|C", "sentence": "<the answer's sentence, exact>", "why": "<what the read does not show, in plain words, short; no read numbers and no tool names>", "would_settle": "<the one read that would settle it>"}]}. An empty list is a good result."""


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


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "\u2026"


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
        stmt = _clip(str(args.get("sql") or json.dumps(args, default=str)), _SQL_MAX_CHARS)
        vals = _clip(_values(prof), _VALUES_MAX_CHARS) if prof else ""
        return (f"[read {number}] plx.sql {stmt}\n"
                f"   scope: {_plain(got.get('scope'))}\n"
                f"   rows: {prof.get('rows', got.get('row_count'))}; columns: {got.get('columns')}; "
                f"limit reached: {prof.get('limit_hit')}; grouped by: {grouped}; "
                f"error: {got.get('error') or got.get('raw')}\n"
                f"   value counts: {vals or 'none computed'}")
    docs = got.get("documents") or got.get("opened") or []
    searched = got.get("searched") if isinstance(got.get("searched"), Mapping) else {}
    comp = got.get("completeness") if isinstance(got.get("completeness"), Mapping) else {}
    widened = searched.get("widened")
    reason = str(comp.get("reason") or "")
    exact_note = ""
    if reason in ("meaning_continue", "attachments_continue"):
        exact_note = f"; exact_matches_complete (exact_returned: {comp.get('returned', 0)}, cursor only continues {reason})"
    widened_line = f"\n   widened: {_clip(str(widened), 300)}" if widened else ""
    return (f"[read {number}] {name} {_clip(json.dumps(args, default=str), _SQL_MAX_CHARS)}\n"
            f"   returned: {len(docs)}{exact_note}; coverage: "
            f"{json.dumps(got.get('coverage') or got.get('completeness'), default=str)}; "
            f"error: {got.get('error')}{widened_line}")


def task_body(question: str, answer: str, events: Sequence[Mapping[str, Any]],
              untraced: Sequence[str] = ()) -> str:
    """Dynamic per-answer input for the covering-read check (sent on stdin while ``PROMPT``
    travels in ``--system-prompt`` for prompt-cache reuse)."""
    # a page of a result already listed is that read again (same statement, more rows)
    cards = [card(n, e) for n, e in enumerate(
        (e for e in _evidence(events) if not _args(e).get("page")))]
    extra = (f"\n\nUNTRACED FIGURES (check whether backed by the reads): "
             f"{json.dumps(list(untraced)[:20], default=str)}" if untraced else "")
    return (f"QUESTION: {question}\n\nANSWER:\n{answer}{extra}\n\nREADS:\n"
            + "\n".join(cards))


def task(question: str, answer: str, events: Sequence[Mapping[str, Any]],
         untraced: Sequence[str] = ()) -> str:
    return PROMPT + "\n\n" + task_body(question, answer, events, untraced=untraced)


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
    """The one call, no tools, bounded time, low effort, and static ``PROMPT`` in
    ``--system-prompt`` for prompt-cache reuse."""
    from operating import humanize
    return humanize.rewriter(binary=getattr(asker, "claude_bin", "claude") or "claude",
                             model=model(), seconds=cover_seconds(), feature=FEATURE,
                             effort=cover_effort(), system_prompt=PROMPT,
                             caller="operator_api.ask_cover")


def check(asker: Any, question: str, answer: str, events: Sequence[Mapping[str, Any]], *,
          call: Optional[Callable[[str], Optional[str]]] = None,
          untraced: Sequence[str] = ()) -> Cover:
    """The flags for ``answer`` (as the session wrote it). Any failure is ``not_checked``
    with why, logged, never raised."""
    started = time.monotonic()
    custom = call or getattr(asker, "_cover_call", None)
    why = None
    try:
        fn = custom or call_for(asker)
        prompt = (task(question, answer, events, untraced=untraced)
                  if custom is not None
                  else task_body(question, answer, events, untraced=untraced))
        for _ in (1, 2):    # one retry of an unreadable reply
            reply = fn(prompt)
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


def sentences(printed: str, cover: Cover) -> list[Mapping[str, Any]]:
    """The listed sentences the answer really holds, each flag with the sentence as the
    answer has it (the check read the answer as the person reads it, references printed). A
    quoted sentence not found is skipped and logged: nothing is flagged on a guess."""
    out = []
    for f in cover.flags:
        at = ask_patch.find(printed, f["sentence"])
        if at is None:
            LOG.info(json.dumps({"ask_cover_unplaced": f["kind"]}))
            continue
        out.append({**f, "sentence": printed[at[0]:at[1]].strip()})
    return out


def patches(written: str, printed: str, cover: Cover,
            events: Sequence[Mapping[str, Any]] = ()
            ) -> tuple[list[ask_patch.Patch], list[dict[str, Any]]]:
    """Map ``cover`` flags (quoted from ``printed``) onto exact ``(start, end)`` spans in
    ``written`` (raw session text with ``{{refs}}`` and chart fences intact).
    Returns ``(matched_patches, unplaced_flags)``."""
    flags = sentences(printed, cover)
    if not flags:
        return [], []
    spans = ask_patch.spans(written)
    rendered_spans: Optional[list[tuple[int, int, str]]] = None
    reasons: dict[tuple[int, int], list[str]] = {}
    asks: dict[tuple[int, int], list[str]] = {}
    unplaced: list[dict[str, Any]] = []
    for f in flags:
        quoted = " ".join(str(f["sentence"] or "").split())
        at = ask_patch.find(written, quoted)
        if at is None and events:
            if rendered_spans is None:
                rendered_spans = [
                    (a, b, " ".join(ask_refs.print_refs(written[a:b], events)[0].split()))
                    for a, b in spans
                ]
            for a, b, s in rendered_spans:
                if quoted == s:
                    at = (a, b)
                    break
            if at is None:
                for a, b, s in rendered_spans:
                    if quoted in s or (s in quoted and len(s) > 12):
                        at = (a, b)
                        break
        if at is None:
            unplaced.append(dict(f))
            continue
        reasons.setdefault(at, []).append(f["why"] or "goes beyond what the reads cover")
        if f["would_settle"]:
            asks.setdefault(at, []).append(f["would_settle"])
    out = [
        ask_patch.Patch(
            a, b, written[a:b],
            "; ".join(dict.fromkeys(reasons[(a, b)])),
            "; ".join(dict.fromkeys(asks.get((a, b), ()))),
        )
        for (a, b) in sorted(reasons)
    ]
    return out, unplaced


def patch_message(patched: Sequence[ask_patch.Patch]) -> str:
    """Targeted sentence-patch turn for ``ask_cover`` flags (PLAN-A2: patch only the
    flagged sentence(s), never rewrite the whole answer or regenerate chart blocks)."""
    return ask_patch.message(
        patched,
        "A check of your answer against what you read lists these sentences: each says "
        "something wider than the reading behind it covers.",
        "For each numbered sentence, narrow it to state only what your existing reads cover, "
        "or run at most one quick read if needed to settle it. Give every figure as a "
        "reference to the result it comes from ({{...}}). Write only the replacement "
        "sentence itself: never frame it as a revision or update, and never mention checks, "
        "reads, tools, or handles. Do not rewrite the rest of your answer or any charts.",
    )


STANDS = "stands"

# Opening phrases that signal a cover-rewritten reply leaked revision/update meta-commentary
# instead of writing a standalone answer for the reader.
_META_OPENINGS: tuple[str, ...] = (
    "i've rewritten",
    "i have rewritten",
    "i've updated",
    "i have updated",
    "i've revised",
    "i have revised",
    "short update:",
    "updated answer",
    "revised answer",
    "corrected answer",
    "here is the updated",
    "here is the revised",
    "here is the corrected",
    "here's the updated",
    "here's the revised",
    "here's the corrected",
    "what changed:",
)

STANDALONE_RETRY = (
    "Your previous reply opened with revision or update commentary instead of a standalone "
    "answer. Write the complete answer again for the person as if answering their question "
    "for the first time: no opening about rewriting, updating, or what changed, and no "
    "mention of checks, reads, search passes, cursors, or filtering steps."
)


def meta_framing(text: str) -> bool:
    """True when ``text`` opens with blatant meta-revision or update framing."""
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    norm = first.lstrip("#*_>`- ").casefold()
    if not norm:
        return False
    if any(norm.startswith(prefix) for prefix in _META_OPENINGS):
        return True
    return "things changed:" in norm or "changes made:" in norm


def message(flags: Sequence[Mapping[str, Any]]) -> str:
    """The turn that sends the flags back. The session writes the whole answer again (the
    host never writes into it): every flagged sentence corrected from the read that settles
    it, every other sentence as it was. ``stands`` alone: every flag proved wrong, the
    answer stays as it is."""
    lines = ["A check of your answer against what you read lists these sentences: each says "
             "something wider than the reading behind it covers."]
    for n, f in enumerate(flags, 1):
        lines.append(f'{n}. "{f["sentence"]}"\n   Why: {f["why"]}'
                     + (f'\n   Settle it with: {f["would_settle"]}' if f["would_settle"] else ""))
    lines.append("For each one run the read that settles it. Then write the complete answer "
                 "again for the person as a standalone reply, as if answering their question "
                 "for the first time: each of these sentences corrected, or kept where the "
                 "read shows it holds, every other sentence as it was. Give every figure as a "
                 "reference to the result it comes from. Write only the answer: never frame "
                 "it as a revision, update, or diff (no \"I've rewritten\", \"Updated answer\", "
                 "\"Short update\", or \"Three things changed\"), no mention of this check, "
                 "of reads, search passes, cursors, filtering steps, reading mechanics, parts, "
                 "tools, handles or references, and nothing about what you did to produce it. "
                 f"If every sentence holds as written, reply with the single word {STANDS}.")
    return "\n".join(lines)
