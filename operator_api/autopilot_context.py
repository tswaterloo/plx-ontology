"""What an autopilot draft takes from the person it is prepared for: their memory
(``operator_api.memory``) and their matching template (``operator_api.templates``).

``operating`` never imports ``operator_api`` (tools/layer_contract.py), so the autopilot
cycle asks ``operating.autopilot.DRAFT_CONTEXT_FACTORY`` for a provider and this module
registers it (``register``; ``python3 -m operator_api.autopilot_context follow|fast ...``
runs the autopilot command line with it, as ``operator_api.learning_memory`` does for the
learner). Without it, or without Postgres, drafts are prepared as before and record nothing.

For each prepared item (``DraftContext.for_draft``):

* **Whose.** The item's owner (a person id) is read under the principals key their own
  memory already uses (``learning_memory.OfflineWriter``: the newest key naming
  ``employee:<oid>``); a person who never used the Ask has no key, so nothing is read.
* **Memory.** Their core memory as ``Memory.core`` gives it (the people exclusion with the
  excluded people's names, re-checked per row; fail closed: unavailable, nothing), with
  every row that cites a source withheld (no read policy decides here, so undecided rows
  never pass). Kept for this draft: an instruction or preference about writing (or sharing
  this draft's words), a commitment or fact about one of the draft's records (or sharing
  its words). At most ``MOST_MEMORY``.
* **Applied.** The kept instructions and preferences change the draft where they state a
  rule (``apply_memory``): a greeting or sign-off ("sign off with Cheers, Sam", "no
  greeting"), a never-write phrase, and "keep it short" (courtesy filler taken out). A line
  with a figure or address is never changed; a draft is never left with nothing to say.
* **Template.** Their own most-used template of the draft's document type (a customer's
  message: ``customer_email``, a supplier's: ``supplier_email``, an order confirmation:
  ``order_confirmation``; else ``phrasing``), visible through ``TemplateStore.list`` (same
  exclusion). Its closing block (the lines after its last blank line: a closing line such
  as "Thanks," then only name lines, no placeholder, figure or sentence) becomes the
  draft's sign-off, replacing the drafter's sign-off and the name under it; a template with
  no such block, a draft already ending with it, or a sign-off the person stated in memory
  (that wins) means it is not used. No template store (schema absent): memory alone.
* **What is recorded** (``used``): ``{"memory": [ids that changed the draft], "related":
  [ids read for it that changed nothing], "template": id or None}``, ids only, kept in the
  item's body, so whoever may read the item reads which entries it used (and resolves them
  only where they may read them: memory is its owner's alone).

Nothing here sends, writes to a system, or keeps any memory or template text.
Evidence: isolated-fixture (``operator_api/tests/test_autopilot_context.py``).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

from operating import learning

from . import memory as mem

# its own name: the served PLX_PG_DSN changes other paths of the autopilot (entity resolution)
DSN_ENV = "PLX_AUTOPILOT_MEMORY_DSN"
MOST_MEMORY = 6
WORDS_SHARED = 0.34
_WRITING = re.compile(
    r"\b(?:repl(?:y|ies|ying)|email|e-mail|draft|write|writing|message|sign(?:-?off|ed|s)?"
    r"|tone|greet\w*|regards|cc|subject|teams|chat|formal|short|brief)\b", re.I)
_SIGN_OFF = re.compile(r"^\s*(?:(?:(?:best|kind|warm|warmest)\s+)?regards|thanks?(?: again| so much)?"
                       r"|thank you(?: again| so much)?|many thanks|cheers|sincerely|best"
                       r"|all the best|best wishes|talk soon|take care)\s*[,!.]?\s*$", re.I)
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\[check:")
_SENTENCE_END = re.compile(r"[.!?]\s*$|\b(?:is|are|was|will|we|you|please|let)\b", re.I)
# a writing instruction asking for shorter replies: the draft's courtesy filler goes
_SHORT = re.compile(r"\b(?:short|shorter|brief|briefer|concise|to the point|keep it tight|"
                    r"fewer words|no (?:filler|pleasantries|small talk))\b", re.I)
# the draft fields an autopilot item may carry, by system: a text the person reads
_TEXT_DRAFTS = ("draft", "teams_draft")


def _draft_text(work: Mapping[str, Any]) -> str:
    parts = [str(work.get("summary") or "")]
    for key in (*_TEXT_DRAFTS, "task", "write"):
        got = work.get(key)
        if isinstance(got, Mapping):
            parts += [str(got.get(k) or "") for k in ("subject", "body", "text", "title",
                                                       "description", "note")]
    for got in work.get("writes") or ():
        if isinstance(got, Mapping):
            parts += [str(got.get(k) or "") for k in ("title", "note", "description")]
    return " ".join(p for p in parts if p)


def _records(work: Mapping[str, Any], event: Mapping[str, Any]) -> set[str]:
    out = {str(event.get("object_id") or "")}
    out.update(str(r) for r in event.get("records") or () if r)
    out.update(str(c) for c in work.get("records") or () if isinstance(c, str))
    return {r for r in out if r}


def doc_type(work: Mapping[str, Any], event: Mapping[str, Any], autopilot: str) -> str:
    """The template type a draft is of (module doc)."""
    if "order" in autopilot and "confirm" in (autopilot + str(work.get("summary") or "")
                                              ).lower():
        return "order_confirmation"
    side = event.get("side")
    if side == "customer":
        return "customer_email"
    if side == "supplier":
        return "supplier_email"
    return "phrasing"


def closing_block(body: str) -> Optional[list[str]]:
    """A template's closing lines (after its last blank line), when they are plain words:
    no placeholder, no figure, at most four short lines; else None."""
    blocks = [b for b in re.split(r"\n\s*\n", str(body or "").strip()) if b.strip()]
    if len(blocks) < 2:
        return None
    lines = [ln.rstrip() for ln in blocks[-1].splitlines() if ln.strip()]
    if not lines or len(lines) > 4 or any(len(ln) > 80 or _PLACEHOLDER.search(ln)
                                          or re.search(r"\d", ln) or "@" in ln
                                          for ln in lines):
        return None
    # a sign-off: it opens with a closing line ("Thanks,", "Best regards,"), and what follows
    # is a name or title (no sentence); a content sentence is never taken as one
    if not _is_sign_off(lines[0]) or any(_SENTENCE_END.search(ln) for ln in lines[1:]):
        return None
    return lines


def _is_sign_off(line: str) -> bool:
    return bool(_SIGN_OFF.match(line) or learning._CLOSE.match(line.strip()))


def _sign_off_tail(lines: Sequence[str]) -> Optional[int]:
    """Where the draft's sign-off starts: a closing line among its last three non-empty
    lines with only name lines (no sentence, no figure) after it; None when it has none."""
    filled = [i for i, ln in enumerate(lines) if ln.strip()]
    for i in reversed(filled[-3:]):
        if _is_sign_off(lines[i]):
            after = [lines[j] for j in filled if j > i]
            if all(len(a) <= 60 and not _SENTENCE_END.search(a) and not re.search(r"\d", a)
                   for a in after):
                return i
            return None
    return None


def with_closing(draft: Mapping[str, Any], closing: Sequence[str]) -> Optional[dict]:
    """The draft with ``closing`` as its sign-off: replacing its sign-off line, or after
    its last line when it has none; None when it already ends with those lines."""
    body = str(draft.get("body") or "").rstrip()
    lines = body.split("\n")
    if [ln.strip() for ln in lines[-len(closing):]] == [c.strip() for c in closing]:
        return None
    tail = _sign_off_tail(lines)
    if tail is not None:          # the drafter's sign-off and any name under it
        lines = lines[:tail]
        while lines and not lines[-1].strip():
            lines.pop()
    out = dict(draft)
    out["body"] = "\n".join([*lines, "", *closing])
    return out


class DraftContext:
    """The provider the autopilot cycle calls once per prepared item (module doc).
    ``keys``: person id -> their memory's reader key (default: as ``OfflineWriter``)."""

    def __init__(self, memory: Any, templates: Any = None,
                 keys: Optional[Callable[[str], Optional[str]]] = None) -> None:
        from .learning_memory import OfflineWriter
        self.memory, self.templates = memory, templates
        self._keys = keys or OfflineWriter(memory)._key_from_memory
        self._held: dict[str, Optional[str]] = {}

    def _key(self, owner: str) -> Optional[str]:
        oid = learning.person_id(owner)
        if not oid or "employee:" in oid or "," in oid:
            return None
        if oid not in self._held:
            try:
                self._held[oid] = self._keys(oid)
            except Exception:  # noqa: BLE001 - memory unreadable: nothing read
                self._held[oid] = None
        return self._held[oid]

    def _memory(self, reader: Any, text: str, records: set[str]) -> list[dict[str, Any]]:
        rows = self.memory.core(reader, decide=lambda handles: {})
        terms = mem._terms(text, most=24)
        out: list[dict[str, Any]] = []
        for row in rows:
            body = f"{row.get('subject') or ''} {row.get('text') or ''}"
            shared = mem._coverage(terms, body) if terms else 0.0
            if row["kind"] in ("instruction", "preference"):
                keep = bool(_WRITING.search(body)) or shared >= WORDS_SHARED
            else:
                keep = bool(records & set(row.get("records") or ())) or shared >= WORDS_SHARED
            if keep:
                out.append(row)
            if len(out) >= MOST_MEMORY:
                break
        return out

    def _template(self, reader: Any, kind: str) -> Optional[tuple[int, list[str]]]:
        if self.templates is None:
            return None
        try:
            rows = self.templates.list(reader, doc_type=kind) or []
        except Exception:  # noqa: BLE001 - no template store (schema absent): memory only
            return None
        for row in rows:
            if not row.get("mine"):
                continue
            closing = closing_block(row.get("body") or "")
            if closing:
                return int(row["id"]), closing
        return None

    def writing(self, owner: str, event: Mapping[str, Any], work: Mapping[str, Any]
                ) -> list[str]:
        """The person's own memory a reply drafted for them should follow (the rows
        ``_memory`` keeps for this message: how they write, and facts about its records),
        as one line each, for the model that writes the reply
        (``operating.autopilot_drafter``). [] when they have no memory key."""
        key = self._key(owner)
        if not key:
            return []
        text = f"{event.get('subject') or ''} {event.get('text') or ''}"
        rows = self._memory(mem.KeyReader(key), text, _records(work, event))
        return [" ".join(f"{r.get('subject') or ''} {r.get('text') or ''}".split())[:400]
                for r in rows]

    def for_draft(self, owner: str, autopilot: str, work: Mapping[str, Any],
                  event: Mapping[str, Any]) -> tuple[dict[str, Any], Optional[dict]]:
        """(the work with the person's memory and template applied, ``used`` or None when
        nothing was kept)."""
        out = dict(work)
        key = self._key(owner)
        if not key:
            return out, None
        reader = mem.KeyReader(key)
        kept = self._memory(reader, _draft_text(work), _records(work, event))
        drafts = [k for k in _TEXT_DRAFTS if isinstance(work.get(k), Mapping)
                  and isinstance(work[k].get("body"), str)]
        applied: list[int] = []
        signed = False
        for name in drafts:
            body, ids, own_sign_off = apply_memory(out[name]["body"], kept, event,
                                                   chat=name == "teams_draft")
            if ids:
                out[name] = {**out[name], "body": body}
                applied += [i for i in ids if i not in applied]
                signed = signed or own_sign_off
        used_template = None
        if drafts and not signed:     # a sign-off the person stated wins over a template's
            got = self._template(reader, doc_type(work, event, autopilot))
            if got is not None:
                for name in drafts:
                    changed = with_closing(out[name], got[1])
                    if changed is not None:
                        out[name] = changed
                        used_template = got[0]
        related = [int(r["id"]) for r in kept if int(r["id"]) not in applied]
        if not applied and not related and used_template is None:
            return out, None
        return out, {"memory": applied, "related": related, "template": used_template}


_PERSON = "draft-owner"


def _lesson(row_id: int, n: int, rule: Mapping[str, str]) -> "learning.Lesson":
    return learning.Lesson(lesson_id=f"st_mem{row_id}_{n}", scope="person", who=_PERSON,
                           kind="style", key=rule["key"], value=rule["value"],
                           confidence=1.0, weight=1.0, total=1.0, seen=1, first_at="",
                           last_at="", evidence=[], text=rule["text"])


def apply_memory(body: str, rows: Sequence[Mapping[str, Any]], event: Mapping[str, Any],
                 *, chat: bool = False) -> tuple[str, list[int], bool]:
    """``body`` with the person's own writing instructions and preferences applied:
    the greeting, sign-off and never-write rules they stated (``learning.stated_rules``,
    applied as stated lessons by ``Lessons.restyle``, which never changes a line with a
    figure or address), and "keep it short" (their draft's courtesy filler taken out, a
    draft never left with nothing to say). Returns (body, the ids of the entries that
    changed it, whether a sign-off of theirs was applied); an entry that changed nothing
    is not in the ids. For one rule the entry ``core`` ranks first wins."""
    writing = [r for r in rows if r.get("kind") in ("instruction", "preference")
               and isinstance(r.get("text"), str)]
    lessons: dict[str, learning.Lesson] = {}
    for row in writing:     # ``core`` ranks first, newest first on ties: the first wins
        for n, rule in enumerate(learning.stated_rules(row["text"])):
            lessons.setdefault(rule["key"], _lesson(int(row["id"]), n, rule))
    applied: list[int] = []
    signed = False
    if lessons and not chat:
        work = body
        if any(k.endswith("signoff") for k in lessons):
            # their sign-off replaces the drafter's whole (the name lines under it too)
            lines = body.split("\n")
            tail = _sign_off_tail(lines)
            if tail is not None:
                work = "\n".join(lines[:tail + 1])
        styled, used = learning.Lessons(lessons.values()).restyle(
            work, _PERSON, (), event=event)
        if used and styled != body:
            body = styled
            for lesson in used:
                rid = int(lesson.lesson_id[len("st_mem"):].split("_")[0])
                if rid not in applied:
                    applied.append(rid)
                signed = signed or lesson.key.endswith("signoff")
    short = next((r for r in writing if _SHORT.search(r["text"])), None)
    if short is not None:
        trimmed = _without_filler(body)
        if trimmed != body:
            body = trimmed
            if int(short["id"]) not in applied:
                applied.append(int(short["id"]))
    return body, applied, signed


def _without_filler(body: str) -> str:
    """The body without its courtesy filler sentences ("Hope you are well.", "Let me know
    if you have any questions."), never the greeting, the sign-off, a line with a figure
    or address, or the last sentence with something to say."""
    from operating import learning_fewshot as fs
    lines = body.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    tail = _sign_off_tail(lines)
    end = tail if tail is not None else len(lines)
    out, dropped, left = list(lines), False, 0
    for i in range(len(lines)):
        ln = lines[i]
        if i == first and learning.greeting_of(ln)[0] != "none" or i >= end or not ln.strip():
            continue
        sentences = [x.strip() for x in learning._SENTENCE.findall(ln) if x.strip()]
        # a sentence with a figure or address is kept word for word; only a courtesy
        # sentence with neither goes
        keep = [x for x in sentences if not fs.courtesy(x) or learning._FIGURE.search(x)
                or learning._ADDRESS.search(x)]
        left += len(keep)
        if len(keep) < len(sentences):
            line = ln
            for x in sentences:
                if x not in keep:       # cut out in place: the rest stays as written
                    line = line.replace(x, "", 1)
            out[i] = re.sub(r"[ \t]{2,}", " ", line).strip()
            dropped = True
    if not dropped or left == 0:
        return body
    # empty lines left by a dropped sentence collapse into one paragraph break
    text = "\n".join(out)
    return re.sub(r"\n\s*\n(?:\s*\n)+", "\n\n", text).strip("\n")


def factory(environ: Optional[Mapping[str, str]] = None) -> Optional[Callable[[], Any]]:
    """The ``autopilot.DRAFT_CONTEXT_FACTORY`` for this process: None without Postgres."""
    env = os.environ if environ is None else environ
    dsn = env.get(DSN_ENV)
    if not dsn:
        return None

    def make() -> Any:
        from .templates import TemplateStore
        memory = mem.Memory.for_dsn(dsn, sync=True)
        memory.names.refresh()      # one read now: a cycle never waits on it later
        return DraftContext(memory, TemplateStore(memory.pool.connection, memory))
    return make


def register(environ: Optional[Mapping[str, str]] = None) -> None:
    from operating import autopilot
    autopilot.DRAFT_CONTEXT_FACTORY = factory(environ)


# The autopilot units read the host's plx.env through this allowlist only (review of
# 686a7abc64): the Graph read app the excluded people's names are read with (memory fails
# closed without them) and the exclusion itself. Any other name in the file, a credential
# added later included, never reaches the autopilot process.
ENV_FILE_ENV = "PLX_AUTOPILOT_ENV_FILE"
ENV_ALLOWED = ("MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
               "MICROSOFT_GRAPH_CLIENT_SECRET", "PLX_EXCLUDED_PEOPLE_OIDS",
               "PLX_EXCLUDED_PEOPLE_FILE")


def allowed_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The ``ENV_ALLOWED`` names of the file ``ENV_FILE_ENV`` names, minus any the process
    already has (the unit's own settings win); {} when unset or unreadable."""
    from operating.ops_notify import read_env_file
    named = str(environ.get(ENV_FILE_ENV) or "").strip()
    if not named:
        return {}
    try:
        found = read_env_file(Path(named), ENV_ALLOWED)
    except (OSError, UnicodeDecodeError):
        return {}
    return {k: v for k, v in found.items() if k not in environ}


def main(argv: Sequence[str] | None = None) -> int:
    from operating import autopilot
    os.environ.update(allowed_env(os.environ))
    register()
    return autopilot.main(argv)


if __name__ == "__main__":
    sys.exit(main())
