"""Private file modes that keep the API's shared-write set working.

A store the API writes (feedback, work log, page cache, briefs) is made private when it is
opened: nobody but its owner reads it. On the host, those stores live in the API's shared-write
set (``deploy/release/api_user_setup.sh``): directories of the shared group with the setgid
bit, written by the jobs' user and by the API's own user. There "private" means owner and
group, never others; and a path the other user created cannot be changed, only checked.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import threading

# held while ``make_absent`` has a new file open: a thread of this process that makes the
# same file waits until it is closed again before it connects to it
_MAKING = threading.Lock()


def make_absent(path: str | os.PathLike[str], mode: int = 0o600) -> bool:
    """Make ``path`` an empty file of ``mode`` when it does not exist; one that exists is
    never opened. True when it was made.

    Why never opened: closing any descriptor of a file drops every POSIX lock this process
    holds on it, and SQLite's locks are such locks. ``os.close(os.open(db))`` in one thread
    while another thread of the process has the database open silently unlocks that
    connection; another process then takes the database for itself, checkpoints and resets
    the write-ahead log under it, and the file is corrupt ("database disk image is
    malformed": the live page cache, after a restart beside the standby). A database is
    connected to only after this returns (a thread making the file holds ``_MAKING`` from
    its open to its close, so none connects in between)."""
    with _MAKING:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            return False
        os.close(fd)
        return True


def keep_shared(path: str | os.PathLike[str], mode: int) -> None:
    """``keep_private`` that never raises: for a file just made in the shared-write set (its
    ``os.open(..., 0o600)`` ignores the umask's group bits, so without this the shared group
    cannot read it and the host's perm check re-runs the setup every minute)."""
    try:
        keep_private(path, mode)
    except OSError:
        pass


def keep_private(path: str | os.PathLike[str], mode: int) -> None:
    """Give ``path`` the private ``mode`` (0o700 for a directory, 0o600 for a file).

    In a shared-group directory (a directory with the setgid bit; for a file, its parent
    directory) the group keeps read/write (and search, for a directory), and the setgid and
    sticky bits stay. A path another user owns is left as it is when it already grants
    others nothing; one that grants others access raises PermissionError."""
    p = Path(path)
    st = os.stat(p)
    is_dir = stat.S_ISDIR(st.st_mode)
    holder = os.stat(p if is_dir else p.resolve().parent)
    want = mode & 0o777
    if holder.st_mode & stat.S_ISGID:
        want |= 0o070 if is_dir else 0o060
        if is_dir:
            want |= st.st_mode & (stat.S_ISGID | stat.S_ISVTX)
    current = st.st_mode & 0o7777
    if current == want:
        return
    if st.st_uid != os.geteuid():
        if current & 0o007:
            raise PermissionError(f"{p} grants others access and is not ours to change")
        return
    os.chmod(p, want)
