"""How the verifier judges a capture check's real ``run_cli`` output, for tests.

:func:`verdict` mirrors ``live_verify.run`` once the command has run. It lives outside the
test modules so a test importing it does not import (and observe) every capture test
module ``test_capture_carried_specs`` drives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from verify import live_verify as lv

ROOT = Path(__file__).resolve().parents[1]


def spec_of(check_id: str) -> dict[str, Any]:
    spec = json.loads((ROOT / "devloop/live-checks" / f"{check_id}.json")
                      .read_text(encoding="utf-8"))
    lv.validate_spec(spec, stem=check_id)
    return spec


def verdict(spec: Mapping[str, Any], exit_code: int, document: Any) -> tuple[str, list[str]]:
    """(``pass`` | ``blocked`` | ``fail``, failures) of ``document`` as ``live_verify.run``
    decides it once the command has run: ``expect``; with ``expect_blocked`` and blocked
    parts, ``expect_blocked`` instead (never a pass); ``complete_when`` fails a run with no
    part that met every expectation."""
    document = document if isinstance(document, Mapping) else None
    failures = lv.check_expectations(spec, exit_code, document)
    parts = lv.blocked_parts(document) if spec.get("expect_blocked") else []
    if parts:
        failures = lv.check_expectations(spec, exit_code, document, spec["expect_blocked"])
    done = lv.completeness(spec, document)
    if not failures and not parts and done.get("complete") is False:
        failures = [f"incomplete {path}" for path in done["incomplete"]]
    return ("fail" if failures else "blocked" if parts else "pass"), failures
