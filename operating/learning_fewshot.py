"""Few-shot drafting from a person's own past: the replies they actually wrote to mail like
this one, retrieved by similarity, shape the draft prepared for them now.

**Examples** are the person's own ``draft_sent`` (a prepared draft and the reply they sent
instead) and ``replied`` (their reply to an item that had no draft) signals
(``operating.learning``): features only — the greeting form, the sign-off, the word count
and the reply's ``shape`` (its sentences in order, each a generic template with no figure,
address, amount, name or finance word, or ``#`` for a sentence that carried one), with
digests of the thread, the sender and the topic words. No message text is stored.

**Retrieval** (``neighbours``): an example scores ``THREAD`` for the same thread, ``SENDER``
for the same counterparty, ``FLOW`` for the same kind of item (autopilot), plus the share
of topic words in common; one below ``MIN_SCORE`` is not like this message (topic words
alone never are). The ``K`` best, recent first on ties, only examples from before now.

**Composition** (``compose``): the draft's own sentences that carry a fact (a figure, an
address, a name) are never changed. Around them, by score-weighted vote of the
neighbours (a share of at least ``AGREE`` on at least ``MIN_VOTE`` weight): the greeting
form and the sign-off they write in such replies; the opening and closing sentences they
write again and again (a template in at least ``REPEAT`` neighbours, never one seen once:
a sentence written once can be a fact of that day); and a drafted generic sentence they
always took out (dropped in ``REPEAT`` neighbours of the same flow, kept in none). An
inserted sentence passes the copy gate first.

**Prompt** (``prompt_block``): the same neighbours and the person's distilled style rules
as plain text for a model drafter; the example replies' words are read at that moment
through ``text_of(signal)`` (the store, the person's own mail), never kept here.

Per person only: an example is only ever the same person's (``Lessons`` keeps them by
person and drops a person the exclusion now covers).
"""

from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import learning as L

THREAD = 3.0
SENDER = 2.0
FLOW = 1.5
MIN_SCORE = 1.5
K = 7
AGREE = 0.5
MIN_VOTE = 1.5
REPEAT = 2
SHARE = 0.5           # ... and in this share of the neighbours whose shape is known
LEARNED_MOST = 2      # learned sentences added to one draft at most
POSITION = L.SHAPE_ENDS          # a lead or close: among a reply's first (last) two sentences
NO_FACT = "#"
EXAMPLE_KINDS = ("draft_sent", "replied")


def event_keys(event: Mapping[str, Any]) -> dict[str, Any]:
    """The digests an event is matched on (never its words)."""
    conv = str(event.get("conversation_id") or "")
    return {"thread": L.digest("thread", conv)[:16] if conv else None,
            "sender": L.sender_key(event.get("sender")),
            "topic": set(L.topic_keys(" ".join(str(event.get(k) or "")
                                               for k in ("subject", "text"))))}


def score(example: L.Signal, keys: Mapping[str, Any], autopilot: Optional[str]) -> float:
    d = example.data
    s = 0.0
    if keys.get("thread") and d.get("thread") == keys["thread"]:
        s += THREAD
    if keys.get("sender") and d.get("sender") == keys["sender"]:
        s += SENDER
    if autopilot and d.get("autopilot") == autopilot:
        s += FLOW
    theirs = set(d.get("topic") or ())
    mine = keys.get("topic") or set()
    if theirs and mine:
        s += len(theirs & mine) / len(theirs | mine)
    return s


def neighbours(examples: Iterable[L.Signal], event: Mapping[str, Any],
               autopilot: Optional[str], *, before: Optional[str] = None, k: int = K
               ) -> list[tuple[float, L.Signal]]:
    """The ``k`` examples most like ``event`` (score, signal), best first; only examples
    from before ``before`` (ISO) when given."""
    keys = event_keys(event)
    scored = []
    for ex in examples:
        if ex.kind not in EXAMPLE_KINDS or (before and str(ex.at) >= before):
            continue
        s = score(ex, keys, autopilot)
        if s >= MIN_SCORE:
            scored.append((s, ex))
    scored.sort(key=lambda x: (-x[0], [-ord(c) for c in str(x[1].at)]))
    return scored[:k]


def _vote(pairs: Iterable[tuple[float, Any]]) -> Optional[Any]:
    """The value with at least ``AGREE`` of the weight, on at least ``MIN_VOTE`` weight."""
    w: Counter = Counter()
    for weight, value in pairs:
        if value is not None:
            w[value] += weight
    total = sum(w.values())
    if total < MIN_VOTE or not w:
        return None
    value, top = max(w.items(), key=lambda kv: (kv[1], str(kv[0])))
    return value if top / total >= AGREE else None


def _repeated(nb: Sequence[tuple[float, L.Signal]]) -> list[tuple[str, bool]]:
    """Generic sentences the neighbours' replies open or close with again and again: each
    template among a reply's first or last ``POSITION`` sentences in at least ``REPEAT``
    neighbours (and ``SHARE`` of those whose shape is known), with whether it opens (its
    mean place in the replies is in their first half); most frequent first."""
    seen: Counter = Counter()
    place: dict[str, list[float]] = defaultdict(list)
    known = 0
    for _s, ex in nb:
        shape = ex.data.get("shape")
        if not isinstance(shape, list) or not shape:
            continue
        known += 1
        ends = sorted(set(range(min(POSITION, len(shape)))) | set(
            range(max(0, len(shape) - POSITION), len(shape))))
        mine: dict[str, float] = {}
        for i in ends:
            t = shape[i]
            if isinstance(t, str) and t and t != NO_FACT and t not in mine:
                mine[t] = i / (len(shape) - 1) if len(shape) > 1 else 0.0
        for t, at in mine.items():
            seen[t] += 1
            place[t].append(at)
    need = max(REPEAT, SHARE * known)
    return [(t, sum(place[t]) / len(place[t]) < 0.5)
            for t, n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])) if n >= need]


def _signoff(data: Mapping[str, Any]) -> Optional[str]:
    """A reply's sign-off line ("" for none; None when not known)."""
    form = data.get("signoff")
    if not form:
        return None
    if form == "none":
        return ""
    literal = data.get("signoff_text") or L.SIGNOFF_TEXT.get(str(form))
    return str(literal) if literal else None


# a learned sentence is inserted only when it is, in full, one of these courtesies: never a
# claim, a promise or an agreement (a sentence written again and again can still say "ok, we
# can do that" or "it shipped yesterday", which is that day's decision or fact, not a style)
_COURTESY = re.compile(
    r"(?:(?:many )?thanks|thank you)(?: so much| very much| again)?"
    r"(?: for (?:your|the) (?:help|email|message|note|update|patience|reply|response|"
    r"quick (?:reply|response)))?"
    r"|(?:please )?let (?:me|us) know if (?:you have any questions|you need anything(?: else)?|"
    r"there are any questions|anything else is needed|you have any other questions)"
    r"|(?:please )?feel free to reach out(?: with any questions)?"
    r"|any questions,? (?:please )?let (?:me|us) know"
    r"|have a (?:great|good|nice|wonderful) (?:day|weekend|evening|week)"
    r"|hope (?:you are|you're|all is) (?:well|doing well)"
    r"|happy to help|much appreciated|appreciate it|appreciate your help"
    r"[.!]?", re.I)
_END = re.compile(r"[.!]?$")


def courtesy(template: str) -> bool:
    """Whether a learned template is, in full, a courtesy the draft may carry (module doc)."""
    text = _END.sub("", template.strip())
    return bool(_COURTESY.fullmatch(text))


def sentence_of(template: str) -> str:
    sentence = re.sub(r"\bi\b", "I", template.strip())
    sentence = sentence[:1].upper() + sentence[1:]
    return sentence if sentence[-1:] in ".!?" else sentence + "."


def _no_fact(line: str) -> bool:
    return not (L._FIGURE.search(line) or L._ADDRESS.search(line))


def compose(body: str, nb: Sequence[tuple[float, L.Signal]], autopilot: Optional[str] = None
            ) -> tuple[str, list[str]]:
    """``body`` in the voice of the neighbours' replies (module doc): (new body, what was
    applied: "greeting", "signoff", "lead", "close", "drop"); the same body and [] when the
    neighbours agree on nothing. A line with a figure or an address is never touched."""
    from . import copy_gate as gate
    if not nb or not isinstance(body, str) or not body.strip():
        return body, []
    lines = body.split("\n")
    applied: list[str] = []

    def first() -> Optional[int]:
        return next((i for i, ln in enumerate(lines) if ln.strip()), None)

    def last() -> Optional[int]:
        return next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)

    def ok(sentence: str) -> bool:
        try:
            return bool(gate.check(sentence, surface="draft_body")["ok"])
        except Exception:  # noqa: BLE001 - unchecked: not added
            return False

    # greeting
    form = _vote((s, ex.data.get("greeting")) for s, ex in nb)
    idx = first()
    if form is not None and idx is not None:
        have, name = L.greeting_of(lines[idx])
        if have != form:
            new = L._greeting_line(form, name)
            if have == "none" and new:
                lines[idx:idx] = [new, ""]
                applied.append("greeting")
            elif have != "none" and new == "":
                del lines[idx]
                while idx < len(lines) and not lines[idx].strip():
                    del lines[idx]
                applied.append("greeting")
            elif have != "none" and new:
                lines[idx] = new
                applied.append("greeting")
    # sentences they always take out of this kind of draft
    same_flow = [(s, ex) for s, ex in nb if ex.kind == "draft_sent"
                 and (autopilot is None or ex.data.get("autopilot") == autopilot)]
    if len(same_flow) >= REPEAT:
        dropped: Counter = Counter()
        kept: set[str] = set()
        for _s, ex in same_flow:
            dropped.update(set(ex.data.get("dropped") or ()))
            kept.update(ex.data.get("kept") or ())
        out_t = {t for t, n in dropped.items() if n >= REPEAT and t not in kept
                 and n >= AGREE * len(same_flow)}
        if out_t:
            trimmed, hit = [], False
            for ln in lines:
                if not _no_fact(ln) or L.greeting_of(ln)[0] != "none" or \
                        L._CLOSE.match(ln.strip()):
                    trimmed.append(ln)
                    continue
                parts = [x.strip() for x in L._SENTENCE.findall(ln) if x.strip()]
                keep = [x for x in parts if L.template(x) not in out_t]
                if len(keep) < len(parts):
                    hit = True
                    if keep:
                        trimmed.append(" ".join(keep))
                    elif trimmed and not trimmed[-1].strip():
                        continue        # no blank line left doubled
                else:
                    trimmed.append(ln)
            content = sum(1 for ln in trimmed if ln.strip() and L.greeting_of(ln)[0] == "none"
                          and not L._CLOSE.match(ln.strip()))
            if hit and content >= 1:
                lines = trimmed
                applied.append("drop")
    text_now = "\n".join(lines).lower()
    chosen = [(t, opens) for t, opens in _repeated(nb)
              if t not in text_now and courtesy(t)][:LEARNED_MOST]
    leads = [sentence_of(t) for t, opens in chosen if opens]
    closes = [sentence_of(t) for t, opens in chosen if not opens]
    leads = [x for x in leads if ok(x)]
    closes = [x for x in closes if ok(x)]
    if leads:
        # after the greeting, in the order they write them
        idx = first()
        at = 0 if idx is None else (idx + 1 if L.greeting_of(lines[idx])[0] != "none" else idx)
        while 0 < at < len(lines) and not lines[at].strip():
            at += 1
        lines[at:at] = leads
        applied.append("lead")
    if closes:
        tail = last()
        if tail is not None and L._CLOSE.match(lines[tail].strip()):
            lines[tail:tail] = [*closes, ""]
        else:
            lines[(tail + 1) if tail is not None else 0:] = ["", *closes]
        applied.append("close")
    # sign-off
    close = _vote((s, _signoff(ex.data)) for s, ex in nb)
    if close is not None:
        tail = last()
        has = tail is not None and bool(L._CLOSE.match(lines[tail].strip()))
        current = lines[tail].strip() if has else ""
        if close != current and (close == "" or _no_fact(close) and L._CLOSE.match(close)):
            if close == "" and has:
                del lines[tail]
                applied.append("signoff")
            elif close and has:
                lines[tail] = close
                applied.append("signoff")
            elif close and tail is not None:
                lines[tail + 1:] = ["", close]
                applied.append("signoff")
    if not applied:
        return body, []
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
    return out, applied


def rules(lessons: Iterable[L.Lesson], at: float = L.APPLY_AT) -> list[str]:
    """The person's distilled style rules as plain lines (greeting, sign-off, length,
    sentences they always add or take out), at ``at`` confidence or more."""
    out = []
    for l in sorted(lessons, key=lambda l: (-l.confidence, l.key)):
        if l.kind == "style" and l.confidence >= at and l.text:
            out.append(l.text)
    return out[:12]


def prompt_block(nb: Sequence[tuple[float, L.Signal]], style: Sequence[str],
                 text_of: Callable[[L.Signal], Optional[tuple[Optional[str], Optional[str]]]],
                 *, most: int = 3, chars: int = 900) -> str:
    """The few-shot block of a drafter prompt: the person's style rules and up to ``most``
    of their own past (draft, what they sent) pairs, most like this message first;
    ``text_of(signal)`` reads (draft, reply) at this moment (None: the example is left
    out). A reply with finance or pay words is never shown. "" when there is nothing."""
    parts = []
    if style:
        parts.append("How this person writes (learned from what they sent):\n"
                     + "\n".join(f"- {r}" for r in style))
    shown = 0
    for _s, ex in nb:
        if shown >= most:
            break
        try:
            got = text_of(ex)
        except Exception:  # noqa: BLE001 - unreadable: not shown
            got = None
        if not got or not got[1]:
            continue
        draft, reply = got
        reply = L.reply_core(reply)[:chars]
        if L.finance_text(reply) or (draft and L.finance_text(draft)):
            continue
        shown += 1
        block = f"Example {shown}:"
        if draft:
            block += f"\nPrepared draft:\n{draft[:chars]}"
        block += f"\nWhat they sent:\n{reply}"
        parts.append(block)
    return "\n\n".join(parts)


def by_person(examples: Iterable[L.Signal]) -> dict[str, list[L.Signal]]:
    out: dict[str, list[L.Signal]] = defaultdict(list)
    for s in examples:
        if s.kind in EXAMPLE_KINDS:
            out[s.person].append(s)
    return out


# ----------------------------------------------------------------------------- model drafter

MODEL_ENV = "PLX_DRAFT_MODEL"         # "claude": the opt-in model drafter (off by default)
_FACT_TOKEN = re.compile(r"[A-Za-z]*\d[\w./:-]*|[\w.%+'-]+@[\w.-]+\.\w{2,}")

DRAFTER_RULE = """You prepare a reply email for a person at the company to review and send.
Rewrite the PREPARED DRAFT the way this person writes such replies: their greeting, sign-off,
length, tone and the phrases they use, learned from what they actually sent before.
Rules: keep every fact of the prepared draft (every number, date, order, PO and address,
exactly as written); add no fact, number, date, name, promise or attachment claim that is not
in the prepared draft or the incoming message; no signature block; plain text only.
Answer with the reply text only."""


def drafter_prompt(draft: str, event: Mapping[str, Any], block: str) -> str:
    """The whole drafter prompt: the rule, the few-shot block (``prompt_block``; "" for a
    zero-shot draft), the incoming message and the prepared draft."""
    incoming = f"Subject: {str(event.get('subject') or '')[:200]}\n" \
               f"{L.reply_core(str(event.get('text') or ''))[:1500]}"
    return "\n\n".join(x for x in (
        DRAFTER_RULE, block, f"INCOMING MESSAGE:\n{incoming}",
        f"PREPARED DRAFT:\n{draft}") if x)


_NAMED = re.compile(r"\b(?:[A-Z][a-z'\-]+|today|tomorrow|yesterday|tonight|monday|tuesday|"
                    r"wednesday|thursday|friday|saturday|sunday|next week|this week)\b")
_COMMON = L._STOP | L._OPENERS | {"hi", "hello", "hey", "dear", "thanks", "regards", "best",
                                  "kind", "cheers", "good", "sincerely", "warm", "sorry",
                                  "attached", "order", "po"}


def facts_kept(draft: str, event: Mapping[str, Any], written: str) -> bool:
    """Whether a model's draft keeps every fact token of the prepared draft (figures,
    record numbers, addresses) and adds none that is in neither the draft nor the incoming
    message: else it is not used (fail closed)."""
    incoming = f"{event.get('subject') or ''}\n{event.get('text') or ''}"
    mine = {t.lower().rstrip(".,") for t in _FACT_TOKEN.findall(draft)}
    known = mine | {t.lower().rstrip(".,") for t in _FACT_TOKEN.findall(incoming)}
    theirs = {t.lower().rstrip(".,") for t in _FACT_TOKEN.findall(written)}
    if not (mine <= theirs and theirs <= known):
        return False
    # no name, weekday or time word that is in neither the draft nor the incoming message
    said = {w.lower() for w in _NAMED.findall(f"{draft}\n{incoming}")}
    return all(w.lower() in said or w.lower() in _COMMON for w in _NAMED.findall(written))


def model_draft(draft: str, event: Mapping[str, Any], block: str,
                call: Callable[[str], Optional[str]]) -> Optional[str]:
    """The model's draft in the person's voice, or None (no answer, a fact lost or added,
    finance or pay words, or the copy gate refuses it): the prepared draft then stands."""
    from . import copy_gate as gate
    try:
        written = call(drafter_prompt(draft, event, block))
    except Exception:  # noqa: BLE001 - no model: the prepared draft stands
        return None
    if not isinstance(written, str) or not written.strip():
        return None
    written = written.strip().strip("`").strip()
    if len(written) > 4 * max(len(draft), 400) or not facts_kept(draft, event, written):
        return None
    if L.finance_text(written) and not L.finance_text(draft):
        return None
    lines = written.split("\n")
    tail = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if tail is not None and L._CLOSE.match(lines[tail].strip()):
        # the person's own sign-off (learned from what they sent), not the company voice's:
        # the copy gate reads the rest
        lines = lines[:tail]
    try:
        if not gate.check("\n".join(lines), surface="draft_body")["ok"]:
            return None
    except Exception:  # noqa: BLE001 - unchecked: not used
        return None
    return written
