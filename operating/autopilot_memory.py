"""The model agent's switches and per-person memory and style for the drafter (split from
``operating.autopilot`` for its size cap; ``autopilot`` re-exports every name)."""

from __future__ import annotations

import datetime as dt
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
MEMORY_LINES = 12
MEMORY_SCOPES = ("user", "role", "domain", "company")
SCOPE_PRECEDENCE: dict[str, int] = {"user": 0, "role": 1, "domain": 1, "company": 2}
KIND_PRECEDENCE: dict[str, int] = {
    "instruction": 0,
    "preference": 1,
    "commitment": 2,
    "fact": 3,
    "style": 4,
}
INACTIVE_STATUSES = frozenset({
    "archived", "deleted", "superseded", "inactive", "expired", "retired", "closed", "done",
})
COMPANY_READERS = frozenset({"company", "org", "company:all"})
_PREF_TAG_RE = re.compile(r"^\[pref\s+\d+\]\s*", re.IGNORECASE)
_EM_DASH_RE = re.compile(r"\s*\u2014\s*")
_LEARNED_PREFIX_RE = re.compile(r"^Learned from their replies:\s*", re.IGNORECASE)


def normalize_memory_text(text: Any, *, max_chars: int = 400) -> str:
    """Clean a single memory line: collapse whitespace, replace em dashes with plain
    hyphens, and cap length."""
    raw = str(text or "")
    if not raw.strip():
        return ""
    cleaned = _EM_DASH_RE.sub(" - ", raw)
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_chars].strip()


def memory_dedupe_key(text: Any) -> str:
    """Case-, whitespace-, and tag-insensitive key for deduplicating memory lines."""
    cleaned = normalize_memory_text(text, max_chars=2000)
    cleaned = _PREF_TAG_RE.sub("", cleaned)
    cleaned = _LEARNED_PREFIX_RE.sub("", cleaned)
    return cleaned.strip(" .,;:!").casefold()


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def is_active_memory_entry(entry: Mapping[str, Any], *, now: Optional[dt.datetime] = None) -> bool:
    """Whether a memory entry is active (not deleted, superseded, archived, expired, or
    empty)."""
    if not isinstance(entry, Mapping):
        return False
    if entry.get("deleted_at") not in (None, "", False):
        return False
    if entry.get("superseded_by") not in (None, "", False):
        return False
    if entry.get("archived") is True:
        return False
    if entry.get("active") is False:
        return False
    status = str(entry.get("status") or "").strip().lower()
    if status and status in INACTIVE_STATUSES:
        return False
    expires = _parse_ts(entry.get("expires_at") or entry.get("valid_until"))
    if expires is not None:
        ref = now or dt.datetime.now(dt.timezone.utc)
        if expires <= ref:
            return False
    return bool(normalize_memory_text(entry.get("text") or ""))


def _lesson_specificity(key: str) -> int:
    if key.startswith("sender:"):
        return 0
    if key.startswith("side:"):
        return 1
    return 2


def learned_style(learned: Any, owner: str, event: Mapping[str, Any]) -> list[str]:
    """The owner's style lessons learned from their own replies and edits of drafts
    (``learning.Lessons``: greeting, sign-off, length, sentences they add or take out),
    for this message's side and sender, ranked most-specific first (sender > side >
    general) and deduplicated case-insensitively; [] when there are none or the lessons
    cannot be read."""
    from . import learning
    try:
        lessons = learned.of(owner) if learned else []
    except Exception:  # noqa: BLE001 - unreadable lessons: none
        return []
    side = str(event.get("side") or "")
    sender = learning.sender_key(str(event.get("sender") or "")) or ""
    matched: list[tuple[int, float, str, str]] = []
    for lesson in lessons:
        key = str(lesson.key)
        if lesson.kind != "style" or lesson.confidence < LEARNED_AT or not lesson.text:
            continue
        if key.startswith("side:") and not key.startswith(f"side:{side}:"):
            continue
        if key.startswith("sender:") and not (sender and key.startswith(f"sender:{sender}:")):
            continue
        text = normalize_memory_text(lesson.text)
        if not text:
            continue
        matched.append((_lesson_specificity(key), -float(lesson.confidence), key, text))
    matched.sort(key=lambda item: (item[0], item[1], item[2]))
    out: list[str] = []
    seen: set[str] = set()
    for _spec, _conf, _key, text in matched:
        dkey = memory_dedupe_key(text)
        if not dkey or dkey in seen:
            continue
        seen.add(dkey)
        out.append(f"Learned from their replies: {text}")
    return out[:8]


def _dedupe_lines(lines: Sequence[Any], *, limit: int = MEMORY_LINES) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        cleaned = normalize_memory_text(raw)
        if not cleaned:
            continue
        dkey = memory_dedupe_key(cleaned)
        if not dkey or dkey in seen:
            continue
        seen.add(dkey)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _writing_style(queue: Queue, held: dict[str, Any], owner: str, mine: Sequence[str],
                   event: Mapping[str, Any], work: Mapping[str, Any]) -> list[str]:
    """How ``owner`` writes, ordered by strict precedence: explicit saved memory rows
    (``provider.writing``) first, then learned style from replies and edits
    (``learned_style``), then mailbox writing rules (``person_style``); [] when none is
    known."""
    out: list[str] = []
    provider = _draft_context(queue, held)
    writing = getattr(provider, "writing", None)
    if writing is not None:
        try:
            out += [str(x) for x in writing(owner, event, work) or ()][:8]
        except Exception:  # noqa: BLE001 - memory never stops a draft
            pass
    out += learned_style(held.get("learned"), owner, event)
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
        raw_lines = _writing_style(queue, held, owner, mine, event,  # type: ignore[arg-type]
                                   work or {})
    except Exception:  # noqa: BLE001 - memory never stops a draft
        return ""
    lines = _dedupe_lines(raw_lines, limit=MEMORY_LINES)
    if not lines:
        return ""
    return ("\nHow this person writes and what they asked for (their own memory, and what "
            "they changed in earlier drafts; follow it unless the message needs otherwise):\n"
            + "\n".join(f"- {x}" for x in lines))


def _owner_tokens(owner_person: str) -> set[str]:
    raw = str(owner_person or "").strip().lower()
    if not raw:
        return set()
    tail = raw.rsplit(":", 1)[-1]
    tokens = {raw}
    if tail:
        tokens.add(tail)
        tokens.add(f"employee:{tail}")
    if not raw.startswith("employee:"):
        tokens.add(f"employee:{raw}")
    return {t for t in tokens if t}


def _reader_matches_owner(reader: Any, owner_tokens: set[str]) -> bool:
    raw = str(reader or "").strip().lower()
    if not raw or not owner_tokens:
        return False
    if raw in owner_tokens:
        return True
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return bool(parts & owner_tokens)


def _infer_scope_and_domain(entry: Mapping[str, Any], owner_tokens: set[str]
                            ) -> tuple[Optional[str], str]:
    """Return ``(scope, entry_domain)`` or ``(None, '')`` when an entry belongs to another
    person and must never be returned."""
    explicit_scope = str(entry.get("scope") or "").strip().lower()
    entry_domain = str(entry.get("domain") or entry.get("role") or "").strip().lower()
    reader = str(entry.get("reader") or entry.get("owner_person") or entry.get("owner") or "").strip().lower()
    subject = str(entry.get("subject") or "").strip().lower()

    if explicit_scope in ("company", "org", "global"):
        return "company", ""
    if explicit_scope in ("role", "domain"):
        if not entry_domain and ":" in reader and reader.startswith(("role:", "domain:")):
            entry_domain = reader.split(":", 1)[1].strip()
        return explicit_scope, entry_domain
    if explicit_scope == "user":
        if reader and not _reader_matches_owner(reader, owner_tokens):
            return None, ""
        return "user", entry_domain

    if reader in COMPANY_READERS:
        return "company", ""
    if reader.startswith(("role:", "domain:")):
        kind_tag, _, dom = reader.partition(":")
        return kind_tag, entry_domain or dom.strip()
    if reader:
        if _reader_matches_owner(reader, owner_tokens):
            return "user", entry_domain
        return None, ""
    if subject.startswith(("company:", "org:")):
        return "company", ""
    if entry_domain:
        return "domain", entry_domain
    return "user", ""


def _query_pg_memory_items(pg: Any, owner_person: str, domain: Optional[str]) -> list[dict[str, Any]]:
    """Read active user + role/domain + company-wide rows from ``memory.item`` in one fast
    parameterized query. Never leaks another user's private rows."""
    if pg is None:
        return []
    reg = pg.execute("SELECT to_regclass('memory.item')").fetchone()
    if not reg or reg[0] is None:
        return []
    owner_tokens = _owner_tokens(owner_person)
    exact_readers = set(COMPANY_READERS) | owner_tokens
    if domain:
        d = str(domain).strip().lower()
        if d:
            exact_readers.add(f"domain:{d}")
            exact_readers.add(f"role:{d}")
    # Also match composite principals keys (e.g. "employee:<oid>,role:sales") strictly on
    # comma-delimited tokens so another user's key is never matched by substring.
    owner_list = sorted(owner_tokens)
    rows = pg.execute(
        "SELECT id, reader, kind, source, at, subject, text, importance, confidence "
        "FROM memory.item "
        "WHERE superseded_by IS NULL AND deleted_at IS NULL "
        "  AND kind IN ('instruction', 'preference', 'commitment', 'fact') "
        "  AND (lower(reader) = ANY(%s) "
        "       OR string_to_array(lower(reader), ',') && %s) "
        "ORDER BY importance DESC, at DESC, id DESC LIMIT 400",
        (sorted(exact_readers), owner_list),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for rid, reader, kind, source, at, subject, text, importance, confidence in rows:
        out.append({
            "id": rid,
            "reader": reader,
            "kind": kind,
            "source": source,
            "at": at,
            "subject": subject or "",
            "text": text,
            "importance": importance,
            "confidence": confidence,
        })
    return out


def _canonical_subject_key(subject: Any) -> str:
    raw = str(subject or "").strip().lower()
    if not raw:
        return ""
    for prefix in ("company:", "org:", "domain:", "role:", "user:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
    return " ".join(raw.split())


def resolve_memory_entries(
    owner_person: str,
    *,
    domain: Optional[str] = None,
    limit: int = MEMORY_LINES,
    pg: Any = None,
    entries: Optional[Sequence[Mapping[str, Any]]] = None,
    company_entries: Optional[Sequence[Mapping[str, Any]]] = None,
    state_dir: Optional[Path] = None,
    held: Optional[dict[str, Any]] = None,
    mine: Sequence[str] = (),
    event: Optional[Mapping[str, Any]] = None,
    work: Optional[Mapping[str, Any]] = None,
    now: Optional[dt.datetime] = None,
) -> list[dict[str, Any]]:
    """Retrieve, filter, rank, and deduplicate active persistent memory entries for
    ``owner_person`` across user, role/domain, and company-wide scopes.

    Precedence (highest wins on conflicts and ordering):
      1. User-specific instructions > preferences > commitments > facts > learned style
      2. Role / domain guidance matching ``domain``
      3. Company-wide preferences and instructions

    Stale, archived, deleted, superseded, inactive, and expired entries are always
    excluded. Duplicate instructions (including case/whitespace/punctuation variants and
    lower-precedence entries sharing the same ``subject``) are collapsed cleanly.
    """
    if limit <= 0:
        return []
    from .read_ceiling import CEILING_ENV
    in_replay = bool(os.environ.get(CEILING_ENV))
    ref_now = now or dt.datetime.now(dt.timezone.utc)
    owner_tokens = _owner_tokens(owner_person)
    wanted_domain = str(domain or "").strip().lower()

    raw_pool: list[Mapping[str, Any]] = []
    if pg is not None and not in_replay:
        raw_pool.extend(_query_pg_memory_items(pg, owner_person, wanted_domain or None))
    if entries:
        raw_pool.extend(entries)
    if company_entries:
        for item in company_entries:
            if isinstance(item, Mapping):
                raw_pool.append({"scope": "company", **item})
    if state_dir is not None and not in_replay:
        local_held = held if held is not None else {}
        queue = _LiveQueue(Path(state_dir))
        if "learned" not in local_held:
            local_held["learned"] = _lessons(queue)   # type: ignore[arg-type]
        try:
            style_lines = _writing_style(queue, local_held, owner_person, mine,
                                         event or {}, work or {})
        except Exception:  # noqa: BLE001 - memory never blocks a reader
            style_lines = []
        for idx, line in enumerate(style_lines):
            cleaned_line = normalize_memory_text(line)
            if not cleaned_line:
                continue
            is_pref = bool(_PREF_TAG_RE.match(cleaned_line))
            raw_pool.append({
                "id": f"style:{idx}",
                "scope": "user",
                "kind": "preference" if is_pref else "style",
                "source": "person_memory",
                "text": _PREF_TAG_RE.sub("", cleaned_line),
                "importance": 0.8 if is_pref else 0.4,
            })

    candidates: list[tuple[tuple[int, int, float, float, int], dict[str, Any]]] = []
    for idx, entry in enumerate(raw_pool):
        if not is_active_memory_entry(entry, now=ref_now):
            continue
        scope, entry_domain = _infer_scope_and_domain(entry, owner_tokens)
        if scope is None:
            continue
        if scope in ("role", "domain"):
            if entry_domain and entry_domain != wanted_domain:
                continue
        kind = str(entry.get("kind") or "preference").strip().lower()
        if kind not in KIND_PRECEDENCE:
            kind = "preference"
        line = normalize_memory_text(entry.get("text") or "")
        if not line:
            continue
        try:
            importance = float(entry.get("importance") if entry.get("importance") is not None else 0.5)
        except (TypeError, ValueError):
            importance = 0.5
        ts = _parse_ts(entry.get("at") or entry.get("updated_at") or entry.get("created_at"))
        ts_epoch = ts.timestamp() if ts is not None else 0.0
        scope_rank = SCOPE_PRECEDENCE.get(scope, 2)
        kind_rank = KIND_PRECEDENCE.get(kind, 3)
        normalized = {
            "id": entry.get("id"),
            "scope": "role" if scope == "domain" else scope,
            "kind": kind,
            "domain": entry_domain,
            "subject": str(entry.get("subject") or "").strip(),
            "text": line,
            "importance": round(importance, 3),
            "at": ts.isoformat() if ts is not None else None,
            "source": str(entry.get("source") or ""),
            "line": line,
        }
        candidates.append(((scope_rank, kind_rank, -importance, -ts_epoch, idx), normalized))

    candidates.sort(key=lambda pair: pair[0])

    kept: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    seen_subjects: set[tuple[str, str]] = set()
    for _rank, item in candidates:
        dkey = memory_dedupe_key(item["text"])
        if not dkey or dkey in seen_text:
            continue
        subj_key = _canonical_subject_key(item["subject"])
        if subj_key:
            # Higher-precedence instruction/preference on the same subject overrides lower-
            # precedence defaults (e.g. user signoff overrides company signoff).
            subj_bucket = ("rule" if item["kind"] in ("instruction", "preference") else item["kind"],
                           subj_key)
            if subj_bucket in seen_subjects:
                continue
            seen_subjects.add(subj_bucket)
        seen_text.add(dkey)
        kept.append(item)
        if len(kept) >= limit:
            break
    return kept


def memory_context_for_reader(
    owner_person: str,
    domain: Optional[str] = None,
    limit: int = MEMORY_LINES,
    *,
    pg: Any = None,
    entries: Optional[Sequence[Mapping[str, Any]]] = None,
    company_entries: Optional[Sequence[Mapping[str, Any]]] = None,
    state_dir: Optional[Path] = None,
    held: Optional[dict[str, Any]] = None,
    mine: Sequence[str] = (),
    event: Optional[Mapping[str, Any]] = None,
    work: Optional[Mapping[str, Any]] = None,
    now: Optional[dt.datetime] = None,
) -> str:
    """Shared persistent-memory context block for BOTH Assist planning and Ask context.

    Returns active user-specific, role/domain, and company-wide preferences cleanly
    formatted in strict precedence order (user > role/domain > company) with zero stale,
    archived, or duplicate entries; returns ``""`` when no active memory applies.
    """
    resolved = resolve_memory_entries(
        owner_person,
        domain=domain,
        limit=limit,
        pg=pg,
        entries=entries,
        company_entries=company_entries,
        state_dir=state_dir,
        held=held,
        mine=mine,
        event=event,
        work=work,
        now=now,
    )
    if not resolved:
        return ""
    sections: list[tuple[str, str]] = [
        ("user", "User instructions and preferences:"),
        ("role", "Role and domain guidance:"),
        ("company", "Company-wide preferences:"),
    ]
    blocks: list[str] = []
    for scope_key, header in sections:
        lines = [item["line"] for item in resolved if item["scope"] == scope_key]
        if not lines:
            continue
        blocks.append(header + "\n" + "\n".join(f"- {ln}" for ln in lines))
    return "\n\n".join(blocks)
