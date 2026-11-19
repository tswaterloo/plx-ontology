"""The person's own Teams sign-in in the app (``integrations.teams_delegated``): so a Teams
reply they confirm posts as them. Only the signed-in person starts, completes, sees or ends
their own; nothing of the kept token ever leaves the server.

* ``GET /api/teams/connection``: connected or not, the scopes granted, when; and what the
  host still lacks for a sign-in (``missing``).
* ``POST /api/teams/connect``: the Microsoft sign-in address (one-time state bound to the
  person, PKCE).
* ``GET /api/teams/callback``: Microsoft's redirect back. A cross-site redirect carries no
  Workbench session, so this route takes none: the signed, single-use, 10-minute state
  ``begin`` made binds it to the person who started it, and the token must be that
  person's own account; then back to the Autopilot page.
* ``DELETE /api/teams/connection``: forget it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import models as m
from .auth import Reader
from .errors import UserFacing


class TeamsConnectMixin:
    """Needs ``AutopilotAreaMixin`` (``_apa_state``, ``_ap_env``)."""

    teams_token_request: Any = None   # tests: the guarded token POST stand-in

    def _tc_who(self, reader: Reader) -> tuple[str, str]:
        """The person's mailbox and Entra object id, as the prepared writes read them."""
        import re
        person = self._pw_person(reader)  # type: ignore[attr-defined]
        email = person.email
        oid = next((i for i in person.ids if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", i)), "")
        if "@" not in email or not oid:
            raise UserFacing("forbidden", "Your sign-in carries no Microsoft identity, so "
                             "Teams cannot be connected for you.", 403)
        return email, oid

    def _tc_store(self) -> Any:
        from integrations import teams_delegated as td
        state = self._apa_state()  # type: ignore[attr-defined]
        if state is None:
            raise UserFacing("action_refused", "Teams sign-in is not configured on this "
                             "server.", 409)
        return td.Store(Path(state))

    def teams_connection(self, reader: Reader) -> m.TeamsConnection:
        from integrations import teams_delegated as td
        email, _oid = self._tc_who(reader)
        got = self._tc_store().status(email)
        return m.TeamsConnection(**got, missing=td.missing(dict(self._ap_env())))  # type: ignore[attr-defined]

    def teams_connect(self, reader: Reader) -> m.TeamsConnectStart:
        from integrations import teams_delegated as td
        email, oid = self._tc_who(reader)
        try:
            url = td.begin(dict(self._ap_env()), self._tc_store(), email, oid)  # type: ignore[attr-defined]
        except td.SignInFailed as failed:
            raise UserFacing("action_refused", "Teams sign-in is not set up on this server "
                             f"yet ({failed.reason}).", 409) from None
        return m.TeamsConnectStart(url=url)

    def teams_callback(self, code: Optional[str], state: Optional[str],
                       error: Optional[str]) -> str:
        """Where the browser goes next: the Autopilot page saying how it ended. No reader:
        the state names whose sign-in it is (``teams_delegated.finish``)."""
        from integrations import teams_delegated as td
        try:
            td.finish(dict(self._ap_env()), self._tc_store(), state or "", code or "",  # type: ignore[attr-defined]
                      request=self.teams_token_request, declined=bool(error))
        except td.SignInFailed as failed:
            return f"/autopilot?teams=failed&why={failed.reason}"
        except UserFacing:
            return "/autopilot?teams=failed&why=not_configured"
        return "/autopilot?teams=connected"

    def teams_disconnect(self, reader: Reader) -> m.TeamsConnection:
        email, _oid = self._tc_who(reader)
        self._tc_store().drop(email)
        return self.teams_connection(reader)
