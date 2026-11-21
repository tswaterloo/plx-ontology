"""The BC live write gate (what a live BC write still lacks), split from
``operating.write_intents`` (which re-exports it) so ``integrations.write_transports``
reads it without an import cycle or an upward import: it sits in the lowest layer
(``tools/layer_contract.py`` LAYERS), since ``write_intents`` may import no
``integrations`` module (test_write_intents NoWriteReach); it imports nothing of the
Workbench."""

from __future__ import annotations

from typing import Any, Mapping

PRODUCTION = "Production"
SANDBOX_ENV = "PLX_BC_SANDBOX_ENVIRONMENT"       # the BC sandbox environment's name
SANDBOX_FLAG = "PLX_WRITES_SANDBOX_LIVE"         # owner's switch for sandbox writes
LIVE_FLAG = "PLX_WRITES_LIVE"                    # owner's switch for production writes
SCOPES_FLAG = "PLX_WRITES_SCOPES_GRANTED"        # "bc" once Vince granted BC write scopes


def live_gate(environ: Mapping[str, str], environment: str, transport: Any) -> list[str]:
    """What a live write to ``environment`` still lacks (empty: it may be sent). The
    sandbox is only the environment ``SANDBOX_ENV`` names, never Production."""
    missing = []
    sandbox = (environ.get(SANDBOX_ENV) or "").strip()
    is_sandbox = bool(sandbox) and sandbox.lower() == environment.lower() \
        and environment.lower() != PRODUCTION.lower()
    if is_sandbox:
        if environ.get(SANDBOX_FLAG) != "1":
            missing.append(f"Sandbox writing is off ({SANDBOX_FLAG}=1 not set)")
    elif environ.get(LIVE_FLAG) != "1":
        missing.append(f"Writing is off ({LIVE_FLAG}=1 not set)")
    if "bc" not in (environ.get(SCOPES_FLAG) or "").replace(",", " ").split():
        missing.append(f"BC write scopes are not granted ({SCOPES_FLAG})")
    if transport is None:
        missing.append("No BC write client exists in this build: the request is recorded, "
                       "not sent")
    return missing
