"""A session host's read policy, kept while what it is decided from is unchanged.

The console keeps one per session (``object_workspace.HostAccess._policy``); a session host
(the MCP server's ``CaseHostTools``, ``keep_policy``) keeps one per reader here by the same
rule, where every call decided it again (a staff reader's mailbox units: ~350 ms a call,
2026-11-18). The policy is decided again when ``host.policy_inputs(actor)`` changed (the
gated actor and its entitlement's fingerprint, the generation, the audiences, both people
exclusions, the capture stores' grant rows, the staff units: a revoked grant applies to the
next call) or past ``POLICY_KEPT_SECONDS``; one holding a transient answer
(``host.policy_settled`` not True), or whose inputs changed while it was decided, is never
kept; no inputs (undecidable now): decided every call."""
from __future__ import annotations

import threading
import time
from typing import Any

# at most this old, for this many readers (the console's ``POLICY_KEPT_SECONDS`` /
# ``KEPT_POLICIES``)
POLICY_KEPT_SECONDS = 120.0
KEPT_POLICIES = 8
_ACTOR = object()   # ``KeptPolicies.policy``'s default key: the actor itself


def same_inputs(held: tuple, now: tuple) -> bool:
    """``held``'s inputs (values, objects, ...) are ``now``'s: equal values, the very same
    objects."""
    return held[0] == now[0] and len(held[1]) == len(now[1]) \
        and all(a is b for a, b in zip(held[1], now[1]))


class KeptPolicies:
    """Per reader (or per session): (values, objects, until, policy)."""

    def __init__(self) -> None:
        self._kept: dict[Any, tuple[tuple, tuple, float, Any]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._kept.clear()

    def policy(self, host: Any, actor: Any, *, key: Any = _ACTOR,
               seconds: float | None = None) -> Any:
        """``host.policy_now(actor)``, kept by the module's rule under ``key`` (default:
        the actor; the console: its session) at most ``seconds`` (default
        ``POLICY_KEPT_SECONDS``). A host without ``policy_inputs`` or ``policy_settled``
        decides it every time."""
        key = actor if key is _ACTOR else key
        inputs = getattr(host, "policy_inputs", None)
        before = inputs(actor) if callable(inputs) else None
        try:
            hash(key)
        except TypeError:
            before = None   # a key that cannot key an entry: decided every time
        if before is not None:
            with self._lock:
                held = self._kept.get(key)
            if held is not None and same_inputs(held, before) \
                    and held[2] > time.monotonic():
                return held[3]
        policy = host.policy_now(actor)
        settled = getattr(host, "policy_settled", None)
        if before is None or not callable(settled) or settled(actor) is not True:
            return policy
        after = inputs(actor)
        if after is None or not same_inputs(before, after):
            return policy
        kept_for = POLICY_KEPT_SECONDS if seconds is None else seconds
        with self._lock:
            self._kept.pop(key, None)
            while len(self._kept) >= KEPT_POLICIES:
                self._kept.pop(next(iter(self._kept)))
            self._kept[key] = (*before, time.monotonic() + kept_for, policy)
        return policy
