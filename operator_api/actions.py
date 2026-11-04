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
                          today=self._today(), environ=env)

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
                confirm_unknown_domains=request.confirm_unknown_domains)
        except ca.ActionRefused as refused:
            raise UserFacing("action_refused", " ".join(refused.problems), refused.status)
        return m.ActionResult(**result)

    def action_states(self, reader: Reader) -> m.ActionStates:
        states = self._desk().states(self._mailbox(reader), reader.display_name, self._today())
        return m.ActionStates(cards={k: m.ActionResult(**v) for k, v in states.items()})

    def actions_log(self, reader: Reader, limit: int = 200) -> m.ActionsLog:
        rows = self._desk().log(self._mailbox(reader), reader.display_name, limit)
        return m.ActionsLog(rows=[m.ActionLogRow(**r) for r in rows])
