"""Backups no night may silently miss (``operating.ops_alarms``, every 5 minutes).

The nightly set's age and the restore test are ``ops_alarms.backup_checks``. These add what
the 11-17/18 nights showed missing: the capture backup skipped the mail store for 27 h, the
nightly set exited 7 on a new unreadable /etc/plx file (and a reset-failed unit forgot it),
and no check read the off-box copy at all. Each is critical (the ``backup`` key prefix) and
never paused (``ops_alarms_pauses.UNPAUSABLE``), so a rebuild window that stops the timers
still pages:

* ``backup_set_partial``: the newest nightly set's manifest lists restore-needed files it
  left out (``config.restore_files_missing``), a dump or a state problem; named.
* ``backup_capture``: the capture backup (``deploy/backup/capture_backup.py``): its last run
  (``last-run.json``) did not exit 0 (refused, skipped pieces, an error: the reason named),
  its manifest lists skipped pieces, or a piece is not confirmed current within
  ``backup_store_hours`` (26: every store backed up in the last 26 h).
* ``backup_offbox``: the off-box copy (``deploy/backup/offbox_copy.py``, ``status.json``) is
  not green or older than ``backup_store_hours``.

Each reads only the backups' own small JSON files (never content). A missing or unreadable
file is a breach (the copy it describes is not known to exist), except the off-box status on
a host where the off-box copy was never set up (no state directory): unknown.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from .ops_health import Check, _check
from .prose_style import some_of

CAPTURE_DIR_ENV = "PLX_CAPTURE_BACKUP_DIR"
CAPTURE_DIR = "/var/backups/plx-capture"
OFFBOX_STATE_ENV = "PLX_OFFBOX_STATE"
OFFBOX_STATE = "~/.local/state/plx-offbox"
LABELS = {"backup_set_partial": "Nightly backup left files out",
          "backup_capture": "Capture stores backup",
          "backup_offbox": "Off-box backup copy"}
_MANIFEST = re.compile(r"^manifest-(\d{8}T\d{6}Z)\.json$")


def _parse(stamp: Any) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _age(now: datetime, then: Optional[datetime]) -> Optional[float]:
    return None if then is None else round(max(0.0, (now - then).total_seconds()) / 3600, 1)


def _read(path: Path) -> Any:
    return json.loads(path.read_text())


def _short(items: Any, n: int = 4) -> str:
    return some_of(list(items), n)


def set_partial_check(root: Optional[Path]) -> list[Check]:
    if root is None or not root.is_dir():
        return []
    found = []
    for sub in ("daily", "weekly"):
        try:
            found += [(m.group(1), root / sub / n) for n in os.listdir(root / sub)
                      if (m := _MANIFEST.match(n))]
        except OSError:
            continue
    if not found:
        return []   # ops_alarms.backup_checks pages a missing set
    stamp, path = max(found)
    try:
        manifest = _read(path)
        missing = list((manifest.get("config") or {}).get("restore_files_missing") or [])
        problems = ([f"dump: {manifest['pg_problem']}"] if manifest.get("pg_problem") else []) \
            + [f"state: {p}" for p in manifest.get("state_problems") or []]
    except (OSError, ValueError, AttributeError, TypeError):
        return [_check("backup_set_partial", None, 0, True, "files",
                       f"the newest set's manifest ({stamp}) is unreadable")]
    if not missing and not problems:
        return [_check("backup_set_partial", 0, 0, False, "files", "")]
    detail = (f"set {stamp}: " + ("; ".join(problems) + "; " if problems else "")
              + (f"left out (unreadable, not staged): {_short(missing)}" if missing else ""))
    return [_check("backup_set_partial", len(missing) + len(problems), 0, True, "files",
                   detail.rstrip("; "))]


def capture_check(now: datetime, bound: float, environ: Mapping[str, str]) -> list[Check]:
    root = Path(environ.get(CAPTURE_DIR_ENV) or CAPTURE_DIR)
    try:
        manifest = _read(root / "manifest.json")
        pieces = manifest["pieces"]
        skipped = dict(manifest.get("skipped") or {})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [_check("backup_capture", None, bound, True, "h",
                       f"no readable capture backup manifest in {root}")]
    try:
        last = _read(root / "last-run.json")
    except (OSError, ValueError):
        last = None   # a run of a release before last-run.json: the manifest says the rest
    completed = _parse(manifest.get("completed_at"))
    # when each piece was last confirmed as its source: a run confirms every piece it did
    # not skip (written, or a shard kept with the same listing)
    ages = {}
    for key, piece in pieces.items():
        piece = piece if isinstance(piece, Mapping) else {}
        when = _parse(piece.get("current_at")) or (
            _parse(piece.get("written_at")) if key in skipped else completed)
        ages[key] = _age(now, when)
    stale = sorted((k for k, a in ages.items() if a is None or a > bound),
                   key=lambda k: -(ages[k] or 1e9))
    worst = max((a for a in ages.values() if a is not None), default=None)
    reasons = []
    if isinstance(last, Mapping) and last.get("code") not in (0, None):
        why = last.get("refused") or last.get("error") or ""
        reasons.append(f"last run exited {last.get('code')}"
                       + (f" ({why})" if why else "")
                       + f" at {last.get('finished_at')}")
    if skipped:
        reasons.append("skipped " + _short(f"{k} ({v})" for k, v in sorted(skipped.items())))
    if stale:
        reasons.append(f"not backed up within {bound:g} h: "
                       + _short(f"{k} ({ages[k]:g} h)" if ages[k] is not None else k
                                for k in stale))
    if not pieces:
        reasons.append("the manifest lists no pieces")
    return [_check("backup_capture", worst, bound, bool(reasons), "h", "; ".join(reasons))]


def offbox_check(now: datetime, bound: float, environ: Mapping[str, str]) -> list[Check]:
    state = Path(os.path.expanduser(environ.get(OFFBOX_STATE_ENV) or OFFBOX_STATE))
    if not state.is_dir():
        return [_check("backup_offbox", None, bound, None, "h", "off-box copy not set up")]
    try:
        status = _read(state / "status.json")
    except (OSError, ValueError):
        return [_check("backup_offbox", None, bound, True, "h", "no off-box copy recorded")]
    age = _age(now, _parse(status.get("finished_at")))
    if status.get("status") != "green":
        problems = status.get("problems") or status.get("error") or status.get("status")
        return [_check("backup_offbox", age, bound, True, "h",
                       f"last off-box copy not green: {_short(problems) if isinstance(problems, list) else problems}")]
    if age is None or age > bound:
        return [_check("backup_offbox", age, bound, True, "h",
                       f"newest off-box copy is {age if age is not None else '?'} h old"
                       f" (to {status.get('host')})")]
    return [_check("backup_offbox", age, bound, False, "h", "")]


def backup_store_checks(now: datetime, t: Mapping[str, float], root: Optional[Path],
                        environ: Mapping[str, str] = os.environ) -> list[Check]:
    bound = float(t.get("backup_store_hours", 26))
    return (set_partial_check(root) + capture_check(now, bound, environ)
            + offbox_check(now, bound, environ))
