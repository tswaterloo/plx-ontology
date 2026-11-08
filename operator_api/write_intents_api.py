"""The app's approve-to-write routes (``/api/writes*``) over ``operating.write_intents``,
shared by the real and the mock backend. The person is always the signed-in reader, the
journal lives in the state directory (in memory without one), current values are read
through the GET-only BC client (tests inject ``write_current``), and no write transport is
ever handed to the desk: every execution is recorded as "would write"."""

from __future__ import annotations

import os
import threading
from typing import Any, Mapping, Optional

from operating import card_actions as ca
from operating import write_intents as wi

from . import models as m
from .auth import Reader
from .errors import UserFacing


class WriteIntentsMixin:
    """Needs ``state_dir``; ``writes_environ``, ``write_current``, ``write_facts`` and
    ``write_grants`` may be set (tests)."""

    writes_environ: Optional[Mapping[str, str]] = None
    write_current: Any = None
    write_facts: Any = None
    write_grants: Any = None
    _write_desk: Any = None
    _write_bc: Any = None
    _write_metadata: Any = None
    _write_lock = threading.Lock()

    def _writes_env(self) -> Mapping[str, str]:
        return os.environ if self.writes_environ is None else self.writes_environ

    def _wdesk(self) -> wi.WriteDesk:
        with self._write_lock:
            if self._write_desk is None:
                self._write_desk = wi.WriteDesk.for_state_dir(getattr(self, "state_dir", None))
            if self._write_metadata is None:
                self._write_metadata = wi.default_metadata()
            return self._write_desk

    @staticmethod
    def _person(reader: Reader) -> wi.Person:
        email = reader.email or next((i for i in sorted(reader.session.identities)
                                      if "@" in i), None)
        return wi.Person(reader.display_name, email, reader.session.subject_oid)

    def _write_ctx(self) -> wi.WriteContext:
        env = self._writes_env()
        self._wdesk()
        return wi.WriteContext(environ=env,
                               exclusion=ca.exclusion_for(env, getattr(self, "state_dir", None)),
                               current=self.write_current or self._read_current,
                               facts=self.write_facts or wi.Facts(),
                               metadata=self._write_metadata, grants=self.write_grants)

    def _read_current(self, environment: str, company_id: str, entity_set: str,
                      record_id: str) -> Optional[dict[str, Any]]:
        """The record now, through the GET-only BC client (None: unreadable here)."""
        from integrations import bc_read
        from integrations.live_source_guard import (LiveSourceAudience, NotVisible,
                                                    SourceUnavailable)
        from ontology.core import Actor
        try:
            if self._write_bc is None:
                api, _, tenant_id, env_name = bc_read.build_bc(self._writes_env())
                audience = LiveSourceAudience({f"bc:{env_name.lower()}:*": ["writes:preview"]},
                                              tenant="writes")
                self._write_bc = (bc_read.BusinessCentralReader(
                    api, audience, tenant_id=tenant_id, environment=env_name), env_name)
            reader, env_name = self._write_bc
            if env_name.lower() != environment.lower():
                return None
            record = reader.get_entity(Actor("writes", frozenset({"writes:preview"}),
                                             "restricted"), company_id, entity_set, record_id)
        except (SourceUnavailable, NotVisible, ValueError, KeyError, TypeError):
            return None
        return {"etag": record["etag"], "fields": record["fields"]}

    def _intent_for(self, reader: Reader, intent_id: str) -> dict[str, Any]:
        intent = self._wdesk().intent(intent_id)
        if intent is None or not self._wdesk().visible_to(intent, self._person(reader),
                                                         self._write_ctx()):
            raise UserFacing("not_found", "That write entry is not on your list.", 404)
        return intent

    @staticmethod
    def _refused(refused: wi.WriteRefused) -> UserFacing:
        return UserFacing("write_refused", " ".join(refused.problems), refused.status)

    def write_prepare(self, reader: Reader, body: m.WritePrepare) -> m.WriteIntentView:
        # A person prepares only what they could approve: the preview shows the record's
        # current values, read for the write path, so they go to grant holders only.
        op = wi.OPERATIONS.get(body.operation) or {}
        person = self._person(reader)
        if not self._write_ctx().granted().holds(person.ids(), body.system,
                                                 op.get("entity_set", "")):
            raise UserFacing("write_refused", f"You hold no write grant for {body.system} "
                             f"{op.get('entity_set', body.operation)}.", 403)
        try:
            found = self._wdesk().prepare(body.model_dump(), person)
        except wi.WriteRefused as refused:
            raise self._refused(refused)
        return m.WriteIntentView(**found)

    def write_list(self, reader: Reader) -> m.WriteIntentList:
        try:
            items = self._wdesk().listing(self._person(reader), self._write_ctx())
        except wi.WriteRefused as refused:
            raise self._refused(refused)
        return m.WriteIntentList(items=[m.WriteIntentView(**i) for i in items])

    def write_preview(self, reader: Reader, intent_id: str) -> m.WritePreview:
        self._intent_for(reader, intent_id)
        try:
            return m.WritePreview(**self._wdesk().preview(intent_id, self._write_ctx()))
        except wi.WriteRefused as refused:
            raise self._refused(refused)

    def write_approve(self, reader: Reader, intent_id: str, body: m.WriteApprove
                      ) -> m.WriteIntentView:
        self._intent_for(reader, intent_id)
        try:
            found = self._wdesk().approve(intent_id, self._person(reader), self._write_ctx(),
                                          confirmed=body.confirm, shown_hash=body.preview_hash,
                                          acknowledge_warnings=body.acknowledge_warnings)
        except wi.WriteRefused as refused:
            raise self._refused(refused)
        return m.WriteIntentView(**found)

    def write_execute(self, reader: Reader, intent_id: str, body: m.WriteExecute
                      ) -> m.WriteIntentView:
        self._intent_for(reader, intent_id)
        try:
            found = self._wdesk().execute(intent_id, self._person(reader), self._write_ctx(),
                                          idempotency_key=body.idempotency_key,
                                          checked_bc=body.checked_bc)
        except wi.WriteRefused as refused:
            raise self._refused(refused)
        return m.WriteIntentView(**found)
