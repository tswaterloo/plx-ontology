"""The app's card actions (``/api/actions/*``) over ``operating.card_actions``, shared by the
real and the mock backend. The card is always re-read as this reader's own suggestion
(never taken from the request), the sender is always the reader's own mailbox, and the
journal lives in the state directory (in memory without one)."""

from __future__ import annotations

from datetime import date
import os
import threading
from typing import Any, Mapping, Optional

from operating import card_actions as ca

from . import models as m
from .auth import Reader
from .errors import UserFacing


class ActionsMixin:
    """Needs ``suggestions(reader)`` and ``state_dir``; ``actions_environ`` and
    ``action_sender`` may be set (tests: a fake Graph)."""

    actions_environ: Optional[Mapping[str, str]] = None
    action_sender: Any = None
    # (mailbox, message id) -> the message's sender and Reply-To; chat id -> its members.
    # Default: read through the GET-only Graph client; tests inject fakes.
    action_thread: Any = None
    action_chat_members: Any = None
    _action_graph: Any = None
    _action_desk: Any = None
    _action_desk_lock = threading.Lock()

    def _desk(self) -> ca.ActionDesk:
        with self._action_desk_lock:
            if self._action_desk is None:
                from integrations.graph_send import ActionSender
                sender = self.action_sender or ActionSender(self._actions_env())
                self._action_desk = ca.ActionDesk(
                    ca.Journal.for_state_dir(getattr(self, "state_dir", None)), sender)
            return self._action_desk

    def _actions_env(self) -> Mapping[str, str]:
        return os.environ if self.actions_environ is None else self.actions_environ

    def _today(self) -> date:
        today = getattr(self, "today", None)
        return today() if callable(today) else date.today()

    @staticmethod
    def _mailbox(reader: Reader) -> Optional[str]:
        """The reader's own address: the signed-in token's, else the session's own."""
        return reader.email or next((i for i in sorted(reader.session.identities)
                                     if "@" in i), None)

    def _action_context(self, reader: Reader) -> ca.Context:
        env = self._actions_env()
        return ca.Context(actor_name=reader.display_name, sender=self._mailbox(reader),
                          sender_oid=reader.session.subject_oid,
                          exclusion=ca.exclusion_for(env, getattr(self, "state_dir", None)),
                          today=self._today(), environ=env,
                          thread=self.action_thread or self._read_thread,
                          chat_members=self.action_chat_members or self._read_chat_members)

    def _graph(self) -> Any:
        """The GET-only Graph client (``integrations.graph_read.build_graph``), or None
        without credentials (a reply or a Teams note then cannot be checked: refused)."""
        if self._action_graph is None:
            from integrations.graph_read import build_graph
            from integrations.live_source_guard import SourceUnavailable
            try:
                self._action_graph = build_graph(self._actions_env())[0]
            except (SourceUnavailable, ValueError):
                return None
        return self._action_graph

    def _read_thread(self, mailbox: str, message_id: str) -> Optional[dict[str, Any]]:
        from integrations import live_source_grammar as grammar
        from integrations.live_source_guard import NotVisible, SourceUnavailable
        api = self._graph()
        if api is None:
            return None
        try:
            raw = api.get_json(grammar.url("graph.message", {"$select": "from,replyTo"},
                                           user=mailbox, message=message_id),
                               headers={"Prefer": 'IdType="ImmutableId"'})
        except (SourceUnavailable, NotVisible, ValueError):
            return None
        return thread_parties(raw)

    def _read_chat_members(self, chat_id: str) -> Optional[list[dict[str, Any]]]:
        from integrations import live_source_grammar as grammar
        from integrations.live_source_guard import NotVisible, SourceUnavailable
        api = self._graph()
        if api is None:
            return None
        try:
            items, completeness = api.get_pages(grammar.url("graph.chat_members", chat=chat_id),
                                                max_pages=5, max_items=250)
        except (SourceUnavailable, NotVisible, ValueError):
            return None
        if completeness.get("truncated"):
            return None
        return [{"id": m.get("userId"), "email": m.get("email")} for m in items]

    def _card(self, reader: Reader, card_id: str) -> dict[str, Any]:
        found = self.suggestions(reader)
        card = ca.card_dict(found.items, card_id) if found.status == "read" else None
        if card is None:
            raise UserFacing("not_found", "That suggestion is no longer on your list.", 404)
        return card

    @staticmethod
    def _edits(request: m.ActionRequest) -> dict[str, Any]:
        return request.payload.model_dump(exclude_none=True) if request.payload else {}

    def action_preview(self, reader: Reader, request: m.ActionRequest) -> m.ActionPreview:
        card = self._card(reader, request.card_id)
        try:
            found = self._desk().preview(card, request.kind, self._edits(request),
                                         self._action_context(reader))
        except ca.ActionRefused as refused:
            raise UserFacing("action_refused", " ".join(refused.problems), refused.status)
        return m.ActionPreview(**{k: v for k, v in found.items() if k != "message_id"})

    def action_confirm(self, reader: Reader, request: m.ActionConfirm) -> m.ActionResult:
        card = self._card(reader, request.card_id)
        try:
            result = self._desk().confirm(
                card, request.kind, self._edits(request), self._action_context(reader),
                confirmed=request.confirm, idempotency_key=request.idempotency_key,
                shown_hash=request.payload_hash,
                confirm_unknown_domains=request.confirm_unknown_domains,
                checked_sent_items=request.checked_sent_items)
        except ca.ActionRefused as refused:
            raise UserFacing("action_refused", " ".join(refused.problems), refused.status)
        return m.ActionResult(**result)

    def action_states(self, reader: Reader) -> m.ActionStates:
        states = self._desk().states(self._mailbox(reader), reader.display_name, self._today())
        return m.ActionStates(cards={k: m.ActionResult(**v) for k, v in states.items()})

    def actions_log(self, reader: Reader, limit: int = 200) -> m.ActionsLog:
        rows = self._desk().log(self._mailbox(reader), reader.display_name, limit)
        return m.ActionsLog(rows=[m.ActionLogRow(**r) for r in rows])


def thread_parties(raw: Any) -> Optional[dict[str, Any]]:
    """A Graph message's sender and Reply-To addresses ({"from", "reply_to"}), or None when
    the answer is not a message (an unreadable party is ``None``: the reply is refused)."""
    if not isinstance(raw, Mapping):
        return None

    def address(value: Any) -> Optional[str]:
        found = (value or {}).get("emailAddress") if isinstance(value, Mapping) else None
        found = found.get("address") if isinstance(found, Mapping) else None
        return found.strip().lower() if isinstance(found, str) and "@" in found else None

    reply_to = raw.get("replyTo")
    parties = [address(v) for v in reply_to] if isinstance(reply_to, list) else None
    return {"from": address(raw.get("from")),
            "reply_to": None if parties is None or None in parties else parties}
