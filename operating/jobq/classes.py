"""Job classes: what each kind of heavy job declares to the admission check.

Numbers measured 2026-11-12 from systemd's "memory peak" journal lines over 14 days:
the declared memory is about the p90 peak, the
amount the job may still grow to. Lower ``priority`` runs first. ``PLX_JOBQ_CLASSES``
names a JSON file of numeric overrides per known class (``{"delta": {"mem_gb": 20}}``);
an unknown class or field there is refused, never added.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from typing import Mapping

OVERRIDES_ENV = "PLX_JOBQ_CLASSES"
SHORT_RUNTIME_S = 600.0         # A short job never waits behind a blocked head


@dataclass(frozen=True)
class JobClass:
    name: str
    priority: int               # lower first: delta 10 < alerts 20 < search 30 < backup 40 < land 50 < tests 60
    mem_gb: float               # declared need (what it may reach)
    cpus: float                 # declared CPUs
    max_concurrent: int         # running at once in this class
    unit: str | None = None     # the systemd service a unit job starts; None: inline only
    preemptible: bool = False   # may be stopped gracefully for a higher class
    max_attempts: int = 3       # unit jobs: then dead letter
    backoff_s: float = 60.0     # first retry delay; doubles per attempt
    backoff_cap_s: float = 1800.0
    cooldown_s: float = 0.0     # a new job of a key waits this long after the key's last run
    preempt_after_s: float = 0.0  # 0: never preempts others; else after this long blocked
    queued: bool = True         # False: never enqueued, only accounted when its unit is active
    max_runtime_s: float = 0.0  # declared longest run; 0: unbounded. Under SHORT_RUNTIME_S a
                                # job may pass a blocked head when it fits beside the floor

    @property
    def short(self) -> bool:
        return 0 < self.max_runtime_s < SHORT_RUNTIME_S

    @property
    def mem_mb(self) -> int:
        return int(self.mem_gb * 1024)


_CLASSES = (
    JobClass("delta", 10, 24, 12, 1, unit="plx-delta.service", cooldown_s=300,
             max_attempts=3, backoff_s=120, preempt_after_s=120),
    JobClass("proactive-whole", 20, 16, 4, 1, unit="plx-proactive-whole.service",
             max_attempts=2, backoff_s=300, preempt_after_s=300),
    JobClass("proactive-inc", 20, 5, 1, 1, unit="plx-proactive-inc.service", cooldown_s=60,
             max_attempts=2, backoff_s=60, preempt_after_s=300),
    JobClass("search-follow", 30, 8, 6, 1, unit="plx-search-follow.service", cooldown_s=60,
             preemptible=True, max_attempts=3, backoff_s=60),
    JobClass("backup", 40, 3, 2, 1, max_attempts=3, backoff_s=600),
    JobClass("land", 50, 1, 1, 1, max_attempts=1, max_runtime_s=540),
    JobClass("tests", 60, 4, 1, 15, preemptible=True, max_attempts=1, max_runtime_s=540),
    # Not queued yet: counted against the headroom while their unit is active.
    JobClass("proactive", 20, 16, 4, 1, unit="plx-proactive.service", queued=False),
    JobClass("proactive-nightly", 20, 16, 4, 1, unit="plx-proactive-nightly.service",
             queued=False),
    JobClass("rag-backfill", 30, 11, 4, 1, unit="plx-rag-backfill.service", queued=False),
    JobClass("search-generation", 30, 8, 4, 1, unit="plx-search-generation.service",
             queued=False),
    JobClass("delta-compact", 40, 12, 2, 1, unit="plx-delta-compact.service", queued=False),
    JobClass("capture-backup", 40, 3, 2, 1, unit="plx-capture-backup.service", queued=False),
    JobClass("truth-check", 40, 3, 1, 1, unit="plx-truth-check.service", queued=False),
)
_NUMERIC = {"priority": int, "mem_gb": float, "cpus": float, "max_concurrent": int,
            "max_attempts": int, "backoff_s": float, "backoff_cap_s": float,
            "cooldown_s": float, "preempt_after_s": float, "max_runtime_s": float}


class UnknownClass(ValueError):
    pass


def load(environ: Mapping[str, str] = os.environ) -> dict[str, JobClass]:
    """Every class by name, with the overrides file applied (ValueError on a bad one)."""
    classes = {c.name: c for c in _CLASSES}
    path = environ.get(OVERRIDES_ENV)
    if not path:
        return classes
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{OVERRIDES_ENV}: not an object")
    for name, fields in data.items():
        if name not in classes or not isinstance(fields, dict):
            raise ValueError(f"{OVERRIDES_ENV}: unknown class {name!r}")
        changes = {}
        for field, value in fields.items():
            kind = _NUMERIC.get(field)
            if kind is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{OVERRIDES_ENV}: {name}.{field} is not a numeric field")
            if value < 0 or (field in ("max_concurrent", "max_attempts") and value < 1):
                raise ValueError(f"{OVERRIDES_ENV}: {name}.{field} out of range")
            changes[field] = kind(value)
        classes[name] = replace(classes[name], **changes)
    return classes


def get(name: str, classes: Mapping[str, JobClass] | None = None) -> JobClass:
    classes = load() if classes is None else classes
    try:
        found = classes[name]
    except KeyError:
        raise UnknownClass(f"unknown job class {name!r}") from None
    if not found.queued:
        raise UnknownClass(f"job class {name!r} is accounted only, never queued")
    return found
