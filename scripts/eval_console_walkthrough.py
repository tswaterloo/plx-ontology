"""Live check ``workspace.console_walkthrough``: the employee walk-through of the fixture
console in headless Chromium (operator_ui/tests/test_console_walkthrough.py, the same walk and checks
script and the same checks), reported pass/fail per step.

Offline: the fixture console (``operator_ui.tests.case_fixtures --fixture``) serves synthetic
rows on 127.0.0.1; no business-system client, no credential (credential-shaped variables are
removed from both children). Prints step names, statuses and counts only. Blocked, with
the reason, where Node, its Playwright package or Chromium is absent (never installed here).
Evidence label: isolated-fixture / injected-transport.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from operator_ui.tests import test_console_walkthrough as walkthrough


def main() -> int:
    out: dict[str, Any] = {"schema": "plx-console-walkthrough/v1",
                           "evidence": "isolated-fixture", "blocked_reason": None}
    if walkthrough.SKIP is not None:
        out["blocked_reason"] = "browser_unavailable"
    else:
        try:
            out.update(walkthrough.step_results(walkthrough.walk_console()))
        except Exception as exc:  # noqa: BLE001 - the walk itself failed: a fail, not blocked
            out.update({"walk_completed": False, "walk_error": type(exc).__name__,
                        "steps": {}, "steps_total": 0, "steps_passed": 0, "steps_failed": 1})
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
