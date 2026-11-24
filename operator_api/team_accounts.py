"""Which Team rows are shared mailboxes or accounts, not people, from the directory's own records.

Two signals, both captured from the tenant, no name list:
* the mailbox kind the directory listing recorded (``mailbox_purpose``: ``shared``);
* an account named only by its address (no display name from Teams or a calendar) that the org
  chart gives no manager, department or job title: nobody is on record behind it.
An account with a staff profile, or a name a colleague gave it, stays a person. A shared
mailbox the tenant types as a personal one and gives a profile (a department, a title) is
not told apart here: that needs the mailbox type of the tenant's own listing."""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple, tuple[frozenset[str], frozenset[str]]]] = {}


def _stamp(*paths: Path) -> tuple:
    out = []
    for p in paths:
        try:
            out.append(p.stat().st_mtime)
        except OSError:
            out.append(None)
    return tuple(out)


def _read(state: Path) -> tuple[frozenset[str], frozenset[str]]:
    """(object ids the mail capture records as shared mailboxes, object ids of org-chart
    accounts with no manager, department or job title)."""
    shared: set[str] = set()
    mail = state / "mail-capture" / "capture.db"
    if mail.is_file():
        try:
            conn = sqlite3.connect(f"file:{mail}?mode=ro", uri=True)
            try:
                shared = {str(o).lower() for (o,) in conn.execute(
                    "SELECT mailbox_id FROM mailbox_purpose WHERE purpose = 'shared'")}
            finally:
                conn.close()
        except sqlite3.Error:
            shared = set()
    bare: set[str] = set()
    try:
        from integrations.capture_org import current_rows
        rows: Mapping[str, Any] = current_rows(state / "org-capture" / "capture.db")
    except Exception:  # noqa: BLE001 - no org chart: nobody is called unprofiled
        rows = {}
    for oid, r in rows.items():
        if r.get("manager_state") == "none" and not r.get("job_title") and not r.get("department"):
            bare.add(str(oid).lower())
    return frozenset(shared), frozenset(bare)


def sets(state_dir: Any) -> tuple[frozenset[str], frozenset[str]]:
    """The two sets, kept until either capture file changes."""
    if not state_dir:
        return frozenset(), frozenset()
    state = Path(state_dir)
    stamp = _stamp(state / "mail-capture" / "capture.db", state / "org-capture" / "capture.db")
    with _LOCK:
        held = _CACHE.get(str(state))
        if held and held[0] == stamp:
            return held[1]
    got = _read(state)
    with _LOCK:
        _CACHE[str(state)] = (stamp, got)
    return got


# Accounts a person confirmed are shared logins (one normal user account several people sign in
# to: the tenant types them as personal, so no captured signal tells them apart). A comma-
# separated list of object ids in the service's environment (deploy/systemd/host/
# plx-api.service.d/team-accounts.conf); Team lists them apart from the people. It changes only
# that listing, never who may read a mailbox (``PLX_SHARED_MAILBOX_ALLOW`` does that).
CONFIRMED_ENV = "PLX_TEAM_SHARED_ACCOUNTS"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def confirmed() -> frozenset[str]:
    """The owner-confirmed shared logins: lower-cased object ids (anything else ignored)."""
    return frozenset(e.strip().lower() for e in os.environ.get(CONFIRMED_ENV, "").split(",")
                     if _GUID.match(e.strip().lower()))


def is_shared(oid: str, row: Mapping[str, Any], state_dir: Any) -> bool:
    oid = oid.lower()
    if oid in confirmed():
        return True
    shared, bare = sets(state_dir)
    return oid in shared or (row.get("name_from") == "address" and oid in bare)
