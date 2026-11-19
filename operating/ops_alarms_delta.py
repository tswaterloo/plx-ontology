"""``delta_step_failed:<step>`` (``operating.ops_alarms``): a whole-store step the delta runs
after its layers in every cycle failed.

The delta's Postgres apply (``operating.pg.ontology_delta.apply_listed``) runs, after the
layers of a cycle, steps of the whole store whose failure leaves the layers applied and the
cycle green: the whole-store link steps (``whole_steps``), the person links
(``person_links``, ``operating.pg.people_scope``) and the people directory
(``people_directory``, ``operating.pg.people_directory_scope``). Each reports
``{"error": ...}`` and keeps its mark, so the next cycle tries again; until one succeeds,
new mail gets no person links (or no Person) and nothing else shows it. The cycle record
(``projection/delta/cycles.jsonl``, ``postgres``) carries each report.

One check per step a recent cycle ran: a breach (high) while the newest cycle that ran it
says it failed, its value the failed runs in a row; recovers when a run succeeds. A step no
cycle in the tail ran is not checked.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from .ops_health import Check, _check

STEPS = ("whole_steps", "person_links", "people_directory")
PREFIX = "delta_step_failed:"
LABEL = "Data refresh step failed"


def delta_step_checks(cycles: Optional[Sequence[Mapping[str, Any]]]) -> list[Check]:
    """The checks of the delta's cycle records (oldest first; None: no delta)."""
    out: list[Check] = []
    for step in STEPS:
        runs = [c["postgres"][step] for c in cycles or ()
                if isinstance(c.get("postgres"), Mapping)
                and isinstance(c["postgres"].get(step), Mapping)]
        if not runs:
            continue
        failed = 0
        for run in reversed(runs):
            if not run.get("error"):
                break
            failed += 1
        error = str(runs[-1].get("error") or "")
        check = _check(PREFIX + step, failed, 0, failed > 0, "runs",
                       f"{step} failed {failed} cycle(s) in a row: {error[:300]}"
                       if failed else "")
        check["level"] = "high"
        out.append(check)
    return out
