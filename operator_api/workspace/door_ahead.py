"""The item gate's read-ahead for a list the door decides at once (``ObjectWorkspace.decide``).

A mail, Teams or file handle is decided by the item gate with three statements of its own
(newest version, the exclusion set's rows, the withheld set); for a list of 2,500 matters
that was 2 s of round trips on Home. The gate's ``prefetch`` (the one a search's candidates
and every page build use) reads them for all handles in a few batched statements and keeps
them in the call's memo. It decides nothing: each handle is still decided by the gate."""
from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

LOG = logging.getLogger(__name__)


def read_ahead(item_gate: Callable | None, actor: Any, handles: Sequence[str],
               schemes: tuple[str, ...]) -> bool:
    """Read ahead the ``schemes`` handles of ``handles`` through ``item_gate.prefetch``.
    True when it ran; False when there was nothing to read or the gate has no prefetch; a
    failing read-ahead is logged and the decisions then read for themselves."""
    prefetch = getattr(item_gate, "prefetch", None)
    wanted = [h for h in handles if h.startswith(schemes)]
    if prefetch is None or not wanted:
        return False
    try:
        prefetch(actor, wanted)
    except Exception:  # noqa: BLE001 - said; each decision then reads for itself
        LOG.warning("door: the item gate's read-ahead failed for %d handles; each is decided "
                    "on its own reads", len(wanted), exc_info=True)
        return False
    return True
