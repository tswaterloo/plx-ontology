"""The app's "My work" routes (``/api/my-work*``) over ``operating.autopilot``: the work the
autopilots prepared for the signed-in reader (and, per the owner's grants, the people of the
teams they manage), with a daily digest and nudges. The queue is the autopilot's own file in
the state directory (``PLX_AUTOPILOT_QUEUE`` overrides). Approve goes through the existing
flows only: a drafted message is an ``autopilot:`` card for ``/api/actions/*`` (preview and
the owner's confirm), a prepared order entry becomes a write intent the autopilot prepared
(``/api/writes*``: preview, a grant holder's approval, dry run). Nothing is sent or written
here."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Optional

from operating import autopilot as ap
from operating import card_actions as ca

from . import models as m
from .auth import Reader
from .errors import UserFacing

QUEUE_ENV = "PLX_AUTOPILOT_QUEUE"


def _principals(reader: Reader) -> str:
    """The reader's key for ``autopilot.visible``: its principals (an employee session holds
    ``employee:<oid>``) and the host-configured subject object id of the session."""
    found = set(getattr(reader.session, "principals", ()) or ())
    oid = getattr(reader.session, "subject_oid", None)
    if oid:
        found.add(f"employee:{oid}")
    return ",".join(sorted(found))


class AutopilotMixin:
    """Needs ``state_dir`` (or ``PLX_AUTOPILOT_QUEUE``); tests may set ``autopilot_queue``
    and ``autopilot_grant`` (``(may_read, held, decide)`` for every reader)."""

    autopilot_queue: Optional[Path] = None
    autopilot_grant: Any = None

    def _ap_env(self) -> Any:
        return os.environ if getattr(self, "writes_environ", None) is None \
            else self.writes_environ  # type: ignore[attr-defined]

    def _ap_queue_path(self) -> Optional[Path]:
        if self.autopilot_queue is not None:
            return Path(self.autopilot_queue)
        env = self._ap_env()
        if env.get(QUEUE_ENV):
            return Path(env[QUEUE_ENV])
        state = getattr(self, "state_dir", None)
        return Path(state) / "autopilot" / ap.QUEUE_FILE if state else None

    def _ap_grants(self) -> dict[str, Any]:
        grants = getattr(self, "_workload_grants", None)
        found = grants() if callable(grants) else None
        return found.load() if found is not None else {"teams": {}, "managers": {}}

    def _ap_visible(self, reader: Reader, everyone: bool = False
                    ) -> Optional[list[dict[str, Any]]]:
        """The reader's visible items; ``everyone`` (the owner-open reader only, decided by
        the caller): every person's items, still under the same audience, marking and
        citation checks."""
        path = self._ap_queue_path()
        if path is None or not path.exists():
            return None
        queue = ap.Queue(path)
        grants = self._ap_grants
        if everyone:
            from operating.proactive_commitments import owner_ids
            mine = owner_ids(_principals(reader))
            owners = {r[0] for r in queue.conn.execute(
                "SELECT DISTINCT owner FROM items WHERE state = 'open'")}

            def grants() -> dict[str, Any]:   # every owner one team the reader holds
                return {"teams": {o: "\x00all" for o in owners},
                        "managers": {o: ["\x00all"] for o in mine}}
        try:
            if self.autopilot_grant is not None:
                may_read, held, decide = self.autopilot_grant
                return ap.visible(queue, reader=_principals(reader), may_read=may_read,
                                  held=held, decide=decide, grants=grants())

            def job(view: Any) -> Any:
                access = getattr(view, "access", None)
                if access is None or not hasattr(access, "grant"):
                    return []          # no grant to check: nothing is shown
                with view._request():
                    may_read, held = access.grant()
                    return ap.visible(queue, reader=_principals(reader), may_read=may_read,
                                      held=held, decide=access.decide,
                                      grants=grants())
            return self.reads.run(reader, job, timeout=60)  # type: ignore[attr-defined]
        finally:
            queue.close()

    def my_work(self, reader: Reader) -> m.MyWork:
        now = datetime.now(timezone.utc)
        path = self._ap_queue_path()
        off = path is not None and (path.parent / ap.KILL_FILE).exists() or \
            str(self._ap_env().get(ap.KILL_ENV, "")).lower() in ("1", "true", "yes", "on")
        items = self._ap_visible(reader)
        if items is None:
            return m.MyWork(status="off" if off else "not_scheduled", items=[],
                            digest=m.MyWorkDigest(**ap.digest([], now)), sent=0, writes=0)
        out = []
        for item in items:
            card = ap.card_of(item) if item["mine"] else None
            out.append(m.MyWorkItem(
                item_id=item["item_id"], autopilot=item["autopilot"],
                label=item["body"].get("label") or item["autopilot"], title=item["title"],
                mine=item["mine"], happened=item["body"].get("happened") or {},
                prepared=item["body"].get("prepared") or {}, citations=item["citations"],
                due_at=item["due_at"], created_at=item["created_at"],
                card_id=card["id"] if card else None,
                offers=[m.CardActionOffer(**o) for o in ca.offers(card)] if card else [],
                write_ready=bool(item["mine"] and ap.write_spec(item))))
        return m.MyWork(status="off" if off else "read", items=out,
                        digest=m.MyWorkDigest(**ap.digest(items, now)), sent=0, writes=0)

    def _own_item(self, reader: Reader, item_id: str) -> dict[str, Any]:
        """An item of the reader's own (a manager sees a report's items but approves none)."""
        if str(self._ap_env().get(ap.KILL_ENV, "")).lower() in ("1", "true", "yes", "on"):
            raise UserFacing("action_refused", "The autopilot is switched off.", 409)
        for item in self._ap_visible(reader) or ():
            if item["item_id"] == item_id and item["mine"]:
                return item
        raise UserFacing("not_found", "That item is not on your list.", 404)

    def my_work_card_of(self, reader: Reader, card_id: str) -> Optional[dict[str, Any]]:
        item_id = card_id[len(ap.CARD_PREFIX):]
        try:
            item = self._own_item(reader, item_id)
        except UserFacing:
            return None
        return ap.card_of(item)

    def my_work_write(self, reader: Reader, item_id: str) -> m.WriteIntentView:
        """The item's order entry as a write intent the autopilot prepared (dry run; a
        person holding the write grant who did not prepare it approves it)."""
        from operating import write_intents as wi
        item = self._own_item(reader, item_id)
        spec = ap.write_spec(item)
        if spec is None:
            raise UserFacing("not_found", "That item has no prepared entry.", 404)
        try:
            found = self._wdesk().prepare(  # type: ignore[attr-defined]
                spec, wi.Person("autopilot", autopilot=item["autopilot"]))
        except wi.WriteRefused as refused:
            raise UserFacing("write_refused", " ".join(refused.problems), refused.status)
        return m.WriteIntentView(**found)
