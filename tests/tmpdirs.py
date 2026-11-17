"""Test temporary directories whose removal outlasts a late writer.

Code under test starts daemon threads it does not join (``case_tools_host.warm_sharepoint``,
``prime_attachment_mailboxes``, a store's background facts read, a host's warm-up): one can
still write into a test's directory while ``shutil.rmtree`` walks it, which fails the test
with ``Directory not empty`` (or ``No such file`` for an entry renamed away mid-walk) only
when the box is loaded (16 test workers, 11-14/11-15). ``remove_settled`` removes the tree
again until a pass finds nothing new; one that never settles raises, naming what is left.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import tempfile
import time

SETTLE_SECONDS = 30.0
_RETRIED = {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}


def remove_settled(path: str | os.PathLike, *, seconds: float = SETTLE_SECONDS) -> None:
    """Remove the tree at ``path``; while a writer adds to it, remove it again (backing off
    from 50 ms to 1 s) for at most ``seconds``. A missing ``path`` is already removed."""
    deadline = time.monotonic() + seconds
    delay = 0.05
    while True:
        try:
            shutil.rmtree(path)
            return
        except OSError as error:
            if not os.path.lexists(path):
                return
            if error.errno not in _RETRIED or time.monotonic() >= deadline:
                left = sorted(str(p.relative_to(path)) for p in Path(path).rglob("*"))
                raise OSError(error.errno, f"{error.strerror}; {path} still holds "
                              f"{left[:20]} after {seconds:g} s") from error
        time.sleep(delay)
        delay = min(delay * 2, 1.0)


class TemporaryDirectory(tempfile.TemporaryDirectory):
    """``tempfile.TemporaryDirectory`` removed by ``remove_settled``."""

    def cleanup(self) -> None:
        self._finalizer.detach()
        remove_settled(self.name)
