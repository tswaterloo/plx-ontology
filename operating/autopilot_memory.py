"""The model agent's switches and per-person memory and style for the drafter (split from
``operating.autopilot`` for its size cap; ``autopilot`` re-exports every name)."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Sequence
from .autopilot_records import _digest
from .autopilot_queue import Queue
from . import autopilot_engine, autopilot_handlers

def _ap():   # late: autopilot imports this module; its names stay patchable there
    from operating import autopilot
    return autopilot


def _draft_context(*args, **kwargs):
    return _ap()._draft_context(*args, **kwargs)


def _lessons(*args, **kwargs):
    return _ap()._lessons(*args, **kwargs)


CARD_EVENT = ("object_id", "channel", "sender", "sender_name", "subject", "cleared",
              "audience", "records", "side", "to", "cc", "conversation_id", "intents")


AGENT_ENV = "PLX_AUTOPILOT_AGENT"


AGENT_PEOPLE_ENV = "PLX_AUTOPILOT_AGENT_PEOPLE"


AGENT_EVENTS = ("message.received", "assistant.look")


def agent_people(environ: Optional[Mapping[str, str]] = None) -> Optional[frozenset[str]]:
    """Who the assistant decides for: None = everyone (``AGENT_ENV`` "1" and no list), else
    the listed ids and addresses, lower-cased (empty: no one)."""
    env = os.environ if environ is None else environ
    listed = frozenset(p.lower() for p in re.split(r"[\s,;]+", str(env.get(AGENT_PEOPLE_ENV)
                                                                 or "")) if p)
    if listed:
        return listed
    return None if str(env.get(AGENT_ENV) or "").strip() == "1" else frozenset()


def agent_mode(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the assistant decides for anyone (``agent_people``)."""
    return agent_people(environ) != frozenset()


def agent_for(owner: str, addresses_of: Mapping[str, Any],
              environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the assistant decides ``owner``'s events: everyone's on, or the list names
    their id (whole, or the object id after the tenant prefix) or one of their addresses."""
    people = agent_people(environ)
    if people is None:
        return True
    own = str(owner or "").lower()
    names = {own, own.rsplit(":", 1)[-1], *(str(a).lower() for a in addresses_of.get(owner, ()))}
    return bool(people & (names - {""}))


def _model_agent(queue: Queue, held: dict[str, Any], summary: Mapping[str, Any],
                 event: Mapping[str, Any], owner: str, work: Mapping[str, Any], people: Any,
                 addresses_of: Mapping[str, Any], checks: Any = None
                 ) -> tuple[dict[str, Any], Optional[str]]:
    """(``work`` with what the assistant prepared, None) or (work, why there is no item):
    the jobber's reasons (``job_model_off``, ``job_busy``, ``job_pending``, ``job_failed``,
    a screen) or ``nothing_to_do``. A question to the person is a ``your_call`` card (its
    options' replies on the usual reply path), a reply the item's draft, changes a job
    (``also_jobs`` beside a reply or question: their own job item); to-dos for a system with
    no write path ride on the item (``todos``).

    A conversation with a reader outside the company (not ``ours_only``): the session has no
    record read tool and writes from the message and that party's own records' lines
    (``autopilot_outbound``); every reply, option and proposed message to an outside reader
    is checked there (``checks`` builds the store's ``Ctx``): a held reply becomes a
    question card saying why, a held message is dropped and named in ``held_back``. The
    drafter's reply checks hold too: a figure in no material, a hollow thanks, a copy tic."""
    from . import autopilot_agent as ag
    from . import autopilot_drafter as ad
    material = work.get("material") if isinstance(work.get("material"), Mapping) else {}
    out = {k: v for k, v in work.items() if k != "material"}
    if held.get("agent_cycle") is not summary:
        held["agent_cycle"] = summary
        if "assistant" not in held:
            held["assistant"] = ag.from_environ(os.environ)
        if held["assistant"] is not None:
            held["assistant"].new_cycle()
    assistant = held.get("assistant")
    if assistant is None:
        return out, "job_model_off"
    key = _digest("agent", event.get("event_id"), owner) if held.get("parallel") else None
    if assistant.busy and not (key and key in assistant.pending()):
        return out, "job_busy"
    text = str(material.get("text") or event.get("text") or "")
    if not text.strip() and event.get("channel") == "teams":
        from . import autopilot_chat as _chat_text
        text = _chat_text.message_text(queue.path.parent.parent, event)
    mine = sorted(addresses_of.get(owner) or ())
    sent_to = {str(a).lower() for a in [*(event.get("to") or ()), *(event.get("cc") or ())]}
    person = next((a for a in mine if a in sent_to), mine[0] if mine else owner)
    from . import autopilot_agent_io as io
    # reads as the person also with an outside reader; the outbound check on the finished
    # text holds what it flags for the person (Guard the output, not the reads)
    described, _inside = io.agent_input(event, text, material, addresses_of)
    described += person_memory(queue.path.parent.parent if queue.path.parent.name ==
                               "autopilot" else None, held, owner, mine, event)
    got, why = assistant.run(event, owner, described, str(person), people, key=key,
                             reads=True)
    if got is None:
        # an invitation the session left nothing on: its answer is still a to-do
        got = io.invitation_only(event, material, why)
    if got is None:
        return out, why
    teams = event.get("channel") == "teams"
    got, held_back, why = io.agent_output(got, event, owner, text, material, addresses_of,
                                          checks, str(person))
    if held_back:
        out["held_back"] = held_back
    if got.empty:
        return out, why
    out.update({"summary": got.summary, "drafted_by": "model"})
    if got.note:
        out["note"] = got.note
    if getattr(got, "warning", ""):
        # the person reads every card before anything goes: a draft a check flagged stays
        # with what to check (and waits for the person: ``autopilot_write.routine``)
        out["warning"] = got.warning
    if got.todos:
        out["todos"] = got.todos
    if got.ask or got.reply:
        if teams:
            out.pop("draft", None)
            out["teams_draft"] = {"channel": "teams",
                                  "conversation": event.get("conversation_id"),
                                  "reply_to": event.get("object_id"), "body": got.reply,
                                  "post": False}
        else:
            out.pop("teams_draft", None)
            base = autopilot_handlers._reply(event, [])
            out["draft"] = {**{k: v for k, v in base.items() if k != "copy_check"},
                            "body": got.reply}
        out["kind"] = "reply"
        if got.ask:
            m = ad.Material(reader="", channel="teams" if teams else "mail",
                            sender=autopilot_engine.sender_label(event.get("sender"),
                                                                 event.get("sender_name")),
                            subject=str(event.get("subject") or ""),
                            text=text[:ad.MESSAGE_CHARS], address=str(person),
                            relation=ad.relation_of(event))
            out["your_call"] = {"ask": got.ask["question"], "facts": [],
                                "options": got.ask["options"], "material": m.kept(),
                                "event": {k: event[k] for k in CARD_EVENT if k in event}}
        out["message"] = {"text": text[:ad.MESSAGE_CHARS]}
        if got.proposals:
            out["also_jobs"] = got.proposals
        return out, None
    out.update({"kind": "job", "jobs": got.proposals})
    return out, None


def aj_described(event: Mapping[str, Any], text: str, linked: Sequence[str]) -> str:
    from . import autopilot_jobs as aj
    return aj.described(event, text, linked)


def ours_only(event: Mapping[str, Any], addresses_of: Mapping[str, Any]) -> bool:
    """Whether every reader of a reply to ``event`` is the company's own (the sender and
    everyone it went to or copied are directory people's addresses, or for Teams a chat with
    no outside member): only then may the draft read the records as its person, so a reply
    never carries another party's records to an outside reader (an outside thread's draft
    reads its own party's account lines only: ``autopilot_drafter.account_lines``). A
    channel (members unknown) or anything unreadable: no."""
    if event.get("side") != "internal":
        return False
    if event.get("channel") == "teams":
        return event.get("outside") == [] and not event.get("unaddressed")
    ours = {str(a).lower() for v in addresses_of.values() for a in (v or ())}
    who = [event.get("sender"), *(event.get("to") or ()), *(event.get("cc") or ())]
    return bool(ours) and all(isinstance(a, str) and a.lower() in ours for a in who)


LEARNED_AT = 0.6     # a style lesson the model drafter is told (``operating.learning``)


def learned_style(learned: Any, owner: str, event: Mapping[str, Any]) -> list[str]:
    """The owner's style lessons learned from their own replies and edits of drafts
    (``learning.Lessons``: greeting, sign-off, length, sentences they add or take out),
    for this message's side and sender, each as "Learned from your replies: ...";
    [] when there are none or the lessons cannot be read."""
    from . import learning
    try:
        lessons = learned.of(owner) if learned else []
    except Exception:  # noqa: BLE001 - unreadable lessons: none
        return []
    side = str(event.get("side") or "")
    sender = learning.sender_key(str(event.get("sender") or "")) or ""
    out: list[str] = []
    for lesson in lessons:
        key = str(lesson.key)
        if lesson.kind != "style" or lesson.confidence < LEARNED_AT or not lesson.text:
            continue
        if key.startswith("side:") and not key.startswith(f"side:{side}:"):
            continue
        if key.startswith("sender:") and not (sender and key.startswith(f"sender:{sender}:")):
            continue
        line = f"Learned from their replies: {lesson.text}"
        if line not in out:
            out.append(line)
    return out[:8]


def _writing_style(queue: Queue, held: dict[str, Any], owner: str, mine: Sequence[str],
                   event: Mapping[str, Any], work: Mapping[str, Any]) -> list[str]:
    """How ``owner`` writes: their writing memory (the draft-context provider's
    ``writing``) and their learned style (``person_style``); [] when neither is known."""
    out: list[str] = []
    out += learned_style(held.get("learned"), owner, event)
    provider = _draft_context(queue, held)
    writing = getattr(provider, "writing", None)
    if writing is not None:
        try:
            out += [str(x) for x in writing(owner, event, work) or ()][:8]
        except Exception:  # noqa: BLE001 - memory never stops a draft
            pass
    if mine:
        from . import person_style
        if "style" not in held:
            held["style"] = person_style.StyleSource(queue.path.parent.parent)
        # the first of the person's addresses whose mailbox gives a style: an alias the
        # exclusion does not decide gives none, and the person's own mailbox still does
        got: Mapping[str, Any] = {}
        for address in mine:
            try:
                got = held["style"](address) or {}
            except Exception:  # noqa: BLE001 - no learned style: plain
                got = {}
            if got.get("rules"):
                break
        out += [str(x) for x in got.get("rules") or ()][:8]
    return out


class _LiveQueue:
    """Where the live queue sits under ``state_dir``: the paths ``_lessons``,
    ``_draft_context`` and ``_writing_style`` read (they never touch a replay's)."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / "autopilot" / "autopilot.sqlite"


MEMORY_LINES = 12


def prime_memory(state_dir: Optional[Path], held: dict[str, Any]) -> None:
    """Makes ``person_memory``'s shared parts once (the lessons, the memory provider, the
    style source), so threads drafting at once only read them."""
    from .read_ceiling import CEILING_ENV
    if state_dir is None or os.environ.get(CEILING_ENV):
        return
    queue = _LiveQueue(Path(state_dir))
    held.setdefault("learned", _lessons(queue))   # type: ignore[arg-type]
    _draft_context(queue, held)                   # type: ignore[arg-type]
    from . import person_style
    held.setdefault("style", person_style.StyleSource(queue.path.parent.parent))


def person_memory(state_dir: Optional[Path], held: dict[str, Any], owner: str,
                  mine: Sequence[str], event: Mapping[str, Any],
                  work: Mapping[str, Any] | None = None) -> str:
    """The person's memory for the assistant's session (Autopilot's memory
    feeds the merged Assist engine): how they write and what they asked for (their memory
    rows), the style learned from their own replies and from how they changed earlier
    drafts (``learning``), and their mailbox's writing rules (``person_style``), as a block
    the session reads beside the message; "" when none is known, for a replay of a past
    moment (``read_ceiling``: today's memory would leak the future) or without a state
    directory. ``held``: kept across one run's lines (the lessons, the provider)."""
    from .read_ceiling import CEILING_ENV
    if state_dir is None or os.environ.get(CEILING_ENV):
        return ""
    queue = _LiveQueue(Path(state_dir))
    if "learned" not in held:
        held["learned"] = _lessons(queue)           # type: ignore[arg-type]
    try:
        lines = _writing_style(queue, held, owner, mine, event,  # type: ignore[arg-type]
                               work or {})
    except Exception:  # noqa: BLE001 - memory never stops a draft
        return ""
    lines = [" ".join(str(x).split())[:400] for x in dict.fromkeys(lines) if str(x).strip()]
    if not lines:
        return ""
    return ("\nHow this person writes and what they asked for (their own memory, and what "
            "they changed in earlier drafts; follow it unless the message needs otherwise):\n"
            + "\n".join(f"- {x}" for x in lines[:MEMORY_LINES]))
