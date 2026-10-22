"""The twin transport: an owner adapter's requests answered by in-process twins.

``TwinTransport`` carries ``operating.action_adapters.OwnerRequest`` objects to the twin
of the request's ``system`` (``PortalTwin.SYSTEM``) and returns its answer
(``{"status", "body"}``). It never resolves a host, opens a socket or reads a credential:
there is no code path from here to any network. It sends only in test mode
(``twin.mode``); its label ``twin-transport`` is admitted only there, and only for this
exact type. Every request is kept in ``requests`` (system, method, path) so a test can
count what reached each twin.
"""

from __future__ import annotations

from typing import Any, Mapping

from .mode import TWIN_LABEL, TwinModeRefused, active


class TwinTransport:
    """Sealed: the twins are fixed at construction."""

    evidence = TWIN_LABEL
    __slots__ = ("_twins", "requests")

    def __init__(self, *twins: Any) -> None:
        systems = {getattr(twin, "SYSTEM", None): twin for twin in twins}
        if None in systems or len(systems) != len(twins):
            raise ValueError("one twin per owning system")
        object.__setattr__(self, "_twins", systems)
        object.__setattr__(self, "requests", [])

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("TwinTransport is sealed")

    def send(self, request: Any) -> Mapping[str, Any]:
        if not active():
            raise TwinModeRefused("not_in_test_mode")
        twin = self._twins.get(request.system)
        if twin is None:
            raise TwinModeRefused("system_not_twinned:" + str(request.system))
        self.requests.append((request.system, request.method, request.path))
        return twin.handle(request)


__all__ = ["TwinTransport"]
