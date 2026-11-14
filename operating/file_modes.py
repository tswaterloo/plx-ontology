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
