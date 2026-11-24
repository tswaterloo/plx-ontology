"""The due-event sources of the accounting and QC autopilots, run by ``autopilot.run_cycle``:
each in its own guard, so one failing (any error) is logged and counted
(``skipped.<name>_due_failed``) and the sources after it still run."""
from __future__ import annotations

from datetime import date
import logging
from typing import Any, Mapping


def events(st: Any, registry: Mapping[str, Any], as_of: date, people: Any,
           skipped: Any) -> list[dict[str, Any]]:
    from . import autopilot_payables as pay
    from . import autopilot_qc_entry as qce
    from . import autopilot_receivables as ar
    out: list[dict[str, Any]] = []
    for name, source in (("receivables", ar), ("qc_entry", qce), ("payables", pay)):
        try:
            out += source.due_events(st, registry, as_of, people)
        except Exception as error:  # noqa: BLE001 - see the module doc
            skipped[f"{name}_due_failed"] += 1
            logging.getLogger(__name__).warning("due events of %s failed: %s: %s", name,
                                                type(error).__name__, str(error)[:200])
    return out
