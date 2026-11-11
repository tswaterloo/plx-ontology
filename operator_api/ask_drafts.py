"""The Ask's drafts as items of the approvals desk, and the records an Ask is linked to.

* **Drafts.** An Ask that was asked to draft something (``operating.general_answer``)
  returns ``AskDraft`` payloads with card ids ``ask:<answer id>:<n>``. The approve-to-send
  desk (``/api/actions/preview`` and ``/api/actions/confirm``, ``operator_api.actions``)
  takes such an id like any card's: an email's card offers the send (recipient, subject
  and body edited in its preview, one explicit confirm); a payment, record change or
  approval is shown in full and offered as a follow-up or an assignment to the person who
  approves it. The card is rebuilt from the reader's own Ask as the work log kept it
  (``operator_api.work_log``, the Ask event's ``detail``), never from the request, and
  only while every source that Ask cites is still open to the reader.
* **Links.** ``linked_records`` names the records an answer is about and those its cited
  sources open as; the work log indexes the Ask event under them, so the record page lists
  the Asks about it and "continue" reopens the session.

Nothing here reads a source or a record; ids, words and the served payload only.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import unquote

_OBJECT_URL = re.compile(r"^/object/([^/?#]+)/([^?#]+)$")


def linked_records(answer: Mapping[str, Any]) -> list[dict[str, str]]:
    """The records an answer is about: its subjects, then any record a source of a fact
    opens as (``/object/<type>/<id>``), once each."""
    out: dict[str, dict[str, str]] = {}
    for ref in answer.get("subjects") or ():
        if isinstance(ref, Mapping) and isinstance(ref.get("object_id"), str):
            out.setdefault(ref["object_id"], {"object_id": ref["object_id"],
                                              "type": str(ref.get("type") or ""),
                                              "label": str(ref.get("label") or "")})
    for fact in answer.get("facts") or ():
        for source in (fact or {}).get("sources") or ():
            match = _OBJECT_URL.match(str((source or {}).get("open_url") or ""))
            if match:
                object_id = unquote(match.group(2))
                out.setdefault(object_id, {"object_id": object_id,
                                           "type": unquote(match.group(1)),
                                           "label": str(source.get("label") or "")})
    return list(out.values())


_HANDLE_KEYS = ("handle", "source_handle", "citation")
_SOURCE_SHAPE = ("quote", "fields")


def cited_handles(answer: Mapping[str, Any]) -> list[str]:
    """Every handle the kept answer would show, once each: its facts' sources and also its
    trail (steps, trails, materials), its conflicts' values and any other part that cites
    one (``handle``, ``source_handle``, ``citation``, ``citations``). A part shaped like a
    source (a system with a quote or fields) with no handle cannot be decided: raises
    ``ValueError`` (the caller withholds the turn)."""
    found: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 40:
            raise ValueError("answer_too_deep")
        if isinstance(node, Mapping):
            cited = [node.get(k) for k in _HANDLE_KEYS if isinstance(node.get(k), str)]
            many = node.get("citations")
            if isinstance(many, (list, tuple)):
                cited += [c for c in many if isinstance(c, str)]
            if not any(cited) and any(node.get(k) not in (None, "", {}, [])
                                      for k in _SOURCE_SHAPE) and "system" in node:
                raise ValueError("source_without_handle")
            found.extend(c for c in cited if c)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value, depth + 1)

    walk(answer, 0)
    return list(dict.fromkeys(found))


# ---- drafts: the approve-to-send desk's cards -----------------------------------------

CARD_PREFIX = "ask:"
_PLACEHOLDER = re.compile(r"\[([^\[\]\n]{1,60})\]")
_DETAIL = re.compile(r"^\s*([A-Za-z][A-Za-z /-]{0,30}):\s*(.+?)\s*$")
ACTION_WORDS = {"payment": "payment", "record_change": "record change",
                "approval": "approval"}
ACTION_KINDS = ("create_follow_up", "assign")   # an action draft: filed or handed on


def _details(body: str) -> list[dict[str, str]]:
    return [{"label": m.group(1).strip(), "value": m.group(2)[:500]}
            for m in map(_DETAIL.match, body.splitlines()) if m][:20]


def draft_card(card_id: str, draft: Mapping[str, Any],
               facts: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The approve-to-send desk's card (``operating.card_actions``) of a kept draft: an
    email's card offers the send (to, subject and body edited in its preview; every
    figure in the sent text either drafted or one of ``facts``, the answer's cited
    values); an action's card offers its local steps (a follow-up, an assignment)."""
    title = str(draft.get("title") or "Draft")[:200]
    card: dict[str, Any] = {"id": card_id, "headline": title, "subject": {"label": title},
                            "action": {"text": title}}
    if draft.get("kind") == "email":
        card["draft"] = {"channel": "email", "to": None,
                         "subject": draft.get("email_subject") or title,
                         "body": str(draft.get("body") or ""), "facts": list(facts)}
        thread = draft.get("in_reply_to")
        if isinstance(thread, str) and thread.startswith("graph-mail://"):
            # a reply to a message in the reader's own mailbox (``operator_api.instruct_api``):
            # the desk offers it in the thread, its recipients read from the message itself
            to = draft.get("to")
            card["draft"]["to"] = {"address": to, "source": thread} \
                if isinstance(to, str) and "@" in to else None
            card["draft"]["facts"].append({"fact": "last_vendor_message", "value": title,
                                           "citation": thread})
    else:
        card["action"] = {"text": " ".join(str(draft.get("body") or "").split())[:500]}
        # a Teams note from the desk carries the drafted words, not the headline
        card["teams_draft"] = {"body": str(draft.get("body") or "")[:4000]}
    return card


def draft_models(answer_id: str, drafts: Iterable[Any]) -> list[dict[str, Any]]:
    """The answer's ``AskDraft`` payloads (as dicts) of ``general_answer.Draft`` items:
    ids ``ask:<answer id>:<n>``, each with the desk's offers and what it lacks."""
    from operating import card_actions as ca
    out = []
    for n, draft in enumerate(drafts):
        card_id = f"{CARD_PREFIX}{answer_id}:{n}"
        holes = list(dict.fromkeys(m.group(1) for m in _PLACEHOLDER.finditer(
            f"{draft.subject}\n{draft.body}")))[:8]
        missing = [f"Fill in: {', '.join(holes)}."] if holes else []
        details = _details(draft.body) if draft.kind != "email" else []
        if draft.kind == "email":
            title = draft.subject or "Draft email"
            kinds = ("send_email",)
            missing.insert(0, "Add the recipient's address before sending.")
        else:
            what = next((d["value"] for d in details if d["label"].lower() == "what"), "")
            title = what or f"Draft {ACTION_WORDS.get(draft.kind, 'action')}"
            kinds = ACTION_KINDS
            missing.append(
                f"The Workbench has no path to carry out this {ACTION_WORDS[draft.kind]} "
                "itself: assign it to the person who approves it, or keep it as a "
                "follow-up.")
        stored = {"id": card_id, "kind": draft.kind, "title": title[:200],
                  "email_subject": draft.subject or None if draft.kind == "email" else None,
                  "body": draft.body, "details": details, "missing": missing, "sent": False}
        offered = [o for o in ca.offers(draft_card(card_id, stored)) if o["kind"] in kinds]
        out.append({**stored, "actions": offered})
    return out


def cited_facts(answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The values the answer's sources state (``Source.fields``), each with its citation:
    what a person may type into an emailed draft beside what was drafted."""
    out = []
    for fact in answer.get("facts") or ():
        for source in (fact or {}).get("sources") or ():
            handle = (source or {}).get("handle")
            for field, value in ((source or {}).get("fields") or {}).items():
                if isinstance(handle, str) and value not in (None, ""):
                    out.append({"fact": str(field), "value": str(value), "citation": handle})
    return out[:60]


class AskDraftsMixin:
    """The desk's cards of Ask drafts, shared by the real and the mock backend.
    ``work_log``: the app's ``WorkLog`` (``operator_api.app`` sets it); ``ask_withheld``
    decides a kept answer's sources for the reader again (the real backend's)."""

    work_log: Any = None

    def ask_withheld(self, reader: Any, handles: list[str]) -> Optional[str]:
        return None

    def kept_answer(self, reader: Any, answer_id: str) -> Optional[dict[str, Any]]:
        """The reader's own Ask answer as the work log kept it (None: none of theirs,
        no log, or the log cannot decide the people exclusion)."""
        store = self.work_log
        if store is None or not answer_id:
            return None
        kept = store.ask_detail(reader.key, answer_id)
        return kept if isinstance(kept, dict) else None

    def ask_draft_card(self, reader: Any, card_id: str) -> Optional[dict[str, Any]]:
        """The desk's card of one of the reader's own Ask drafts; None when it is not
        theirs, not kept, or a source its Ask cites is not open to them now (or cannot be
        decided: fail closed)."""
        if not card_id.startswith(CARD_PREFIX):
            return None
        answer_id = card_id[len(CARD_PREFIX):].rpartition(":")[0]
        answer = self.kept_answer(reader, answer_id)
        draft = next((d for d in (answer or {}).get("drafts") or ()
                      if isinstance(d, Mapping) and d.get("id") == card_id), None)
        if answer is None or draft is None:
            return None
        try:
            if self.ask_withheld(reader, cited_handles(answer)):
                return None
            # an instruction's meetings are re-checked by their own rule
            # (``operator_api.instruct_api``)
            again = getattr(self, "instruction_withheld", None)
            if again is not None and again(reader, answer):
                return None
        except Exception:  # noqa: BLE001 - undecidable: no card
            return None
        return draft_card(card_id, draft, cited_facts(answer))


__all__ = ["AskDraftsMixin", "CARD_PREFIX", "cited_facts", "cited_handles", "draft_card",
           "draft_models", "linked_records"]
