"""A record page's request to the resident plan runner: "answer this record now".

A page whose section has no stored row is answered ``not_run`` (no plan runs in the request);
the record is asked for here. The request is one small file per record in a spool folder of the
app's state (``PLX_PLAN_REQUESTS``, default ``<state>/plan-requests``); the box's puller
(``operating.pg.plan_sections_pull``) sends the ids to the runner's queue on the Spark each round
and removes the files, and the runner answers them whatever their state (``plan_resident.forced``).
A request that cannot be written is logged and reported (the section says ``requested: false``).
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

LOG = logging.getLogger("operating.plan_requests")
ENV = "PLX_PLAN_REQUESTS"
MAX_ID = 700
# the puller runs as another user of the state's group and must list, read and remove what the
# app wrote (KNOWN-GAPS O4: a 2700 folder gave the runners no work): group read-write, setgid
FOLDER_MODE, FILE_MODE = 0o2770, 0o660


def _folder_ready(where: str) -> None:
    """The spool folder exists with the group mode (made so, or repaired when it is not)."""
    os.makedirs(where, mode=FOLDER_MODE, exist_ok=True)   # the umask and the kernel may drop bits
    if (os.stat(where).st_mode & 0o7777) != FOLDER_MODE:
        os.chmod(where, FOLDER_MODE)


def folder() -> str:
    got = os.environ.get(ENV)
    if got:
        return got
    return os.path.join(os.environ.get("PLX_WORKBENCH_STATE_DIR", "/data/plx-state"), "plan-requests")


def request(record_id: str) -> bool:
    """Ask the runner for ``record_id``. True when the request is waiting (made now or already
    there); False when it could not be made (said in the log and in the section)."""
    if not isinstance(record_id, str) or not record_id or len(record_id) > MAX_ID \
            or "\n" in record_id:
        LOG.warning("plan request refused: not a record id")
        return False
    name = hashlib.sha1(record_id.encode("utf-8")).hexdigest() + ".req"
    where = folder()
    path = os.path.join(where, name)
    try:
        _folder_ready(where)
        if os.path.exists(path):
            return True
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(record_id + "\n")
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        LOG.warning("plan request not written (%s): %s", type(exc).__name__, exc)
        return False


def waiting(where: Optional[str] = None) -> list[tuple[str, str]]:
    """(file name, record id) of every request waiting in the spool."""
    where = where or folder()
    out: list[tuple[str, str]] = []
    try:
        names = sorted(n for n in os.listdir(where) if n.endswith(".req"))
    except FileNotFoundError:
        return out
    for n in names:
        try:
            with open(os.path.join(where, n), encoding="utf-8") as fh:
                rid = fh.read().strip()
        except OSError as exc:
            LOG.warning("plan request %s unreadable: %s", n, exc)
            continue
        if rid:
            out.append((n, rid))
    return out


def done(names: list[str], where: Optional[str] = None) -> None:
    where = where or folder()
    for n in names:
        try:
            os.remove(os.path.join(where, n))
        except FileNotFoundError:
            pass
