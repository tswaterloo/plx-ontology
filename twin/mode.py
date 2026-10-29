"""Test mode: the one context in which the write twin's transport is admitted.

``test_mode(allow=...)`` is a process-wide context (re-entrant). While it is active:

* an owner adapter accepts a transport labelled ``TWIN_LABEL`` (``twin-transport``), and
  only one of the exact type ``twin.transport.TwinTransport``, which answers from the
  in-process twins and has no network code (``operating.action_adapters``);
* the allow switch of each capability named in ``allow`` reads as on for that transport
  only (``operating.action_adapters.allow_switch_on``: at dispatch any other transport,
  injected or a reviewed live one in the same process, still needs the real switch, and
  a view or ``status``, which names no transport, shows it off), so the action path's
  real-system gates run unchanged and the twin stands in for the person who turns a
  switch on.

Outside it the twin label is refused like any other unreviewed label. The context refuses
to start while a credential is set in the environment (``credentialed``): a host that can
reach Petra systems never runs in test mode, so a twin outcome can never be mistaken for a
live one there. Nothing here reaches the network.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Iterable, Iterator, Mapping

from devloop.credential_names import KEYLESS, SHELL_KEEP

TWIN_LABEL = "twin-transport"
# A credential is a name the keyless helper strips (devloop.credential_names.KEYLESS, the
# gate's pattern: keys, tokens, secrets, passwords and database URLs), less SHELL_KEEP:
# git's injected config and the GitHub token, which are not business credentials. A new
# key (BRAIN_API_KEY, REPORTING_DATABASE_URL, ...) needs no second list here.

_depth = 0
_allowed: list[frozenset[str]] = []


class TwinModeRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def credentialed(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names of the credentials set (non-empty) in ``environ``."""
    env = os.environ if environ is None else environ
    return sorted(name for name, value in env.items()
                  if value and KEYLESS.match(name) and not SHELL_KEEP.match(name))


@contextmanager
def test_mode(allow: Iterable[str] = ()) -> Iterator[None]:
    """Run the enclosed block in test mode (see the module docstring)."""
    global _depth
    if credentialed():
        raise TwinModeRefused("credentialed_host")
    names = frozenset(allow)
    if any(not isinstance(name, str) or not name for name in names):
        raise TwinModeRefused("allow_must_name_capabilities")
    _depth += 1
    _allowed.append(names)
    try:
        yield
    finally:
        _allowed.pop()
        _depth -= 1


def active() -> bool:
    return _depth > 0


def allowed(capability: str) -> bool:
    """Whether the active test mode turned on ``capability``'s allow switch."""
    return active() and any(capability in names for names in _allowed)


def require() -> None:
    if not active():
        raise TwinModeRefused("not_in_test_mode")


__all__ = ["TWIN_LABEL", "TwinModeRefused", "active", "allowed", "credentialed", "require",
           "test_mode"]
