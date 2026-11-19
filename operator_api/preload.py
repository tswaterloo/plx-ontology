"""Work every first read would otherwise pay once per process, done at API start.

The deployed code's fingerprint (``operating.derived_status.deployed``: what the gap
inbox, the briefing and the kept pages are stamped with) is parsed from the source tree
once per process and tree: 15-40 s cold. The first briefing read paid it on the reader's
request (``proactive_engine_inbox.engine_code``), so Watching's "What fired" and "Data
health" waited on it. It is computed here on a background thread as the API starts, for
every artifact at once (in child processes, all cores), and kept as before (in memory and
in the tree's cache file). Before it, the compiled ontology schema (every tool table, the
access policy and the briefing read it: compiled once per process, seconds under load)
and the people exclusion the access policy is decided with are read the same way. Nothing
here reads any reader's data: no permission is decided, and a failure only leaves the
work to the first read, as before.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


def code_fingerprint(state_dir: Any = None) -> None:
    from operating import derived_status
    derived_status.deployed(state_dir=state_dir)


def schema(state_dir: Any = None) -> None:
    """The compiled schema, kept in ``ontology.schema``'s process cache (what
    ``OntologyTools.default_schema`` and every host's schema load from)."""
    from operating.ontology_tools import shared_schema
    shared_schema()


def access_inputs(state_dir: Any = None) -> None:
    """The people exclusion every reader's policy is decided with (``policy_inputs``):
    the same list for every reader, no grant decided."""
    from operating.pg.permissions import load_exclusion
    load_exclusion()


STEPS: tuple[tuple[str, Callable[..., None]], ...] = (
    ("schema", schema), ("access inputs", access_inputs),
    ("code fingerprint", code_fingerprint))


def run(state_dir: Any = None) -> None:
    """Each step once, one after another; a step that fails is logged and left."""
    for name, step in STEPS:
        began = time.monotonic()
        try:
            step(state_dir)
        except Exception as error:  # noqa: BLE001 - the first read does it instead
            log.warning("preload %s failed: %s", name, type(error).__name__)
            continue
        log.info("preloaded %s in %.1f s", name, time.monotonic() - began)


def start(state_dir: Any = None) -> Optional[threading.Thread]:
    """``run`` on a daemon thread (the API serves meanwhile)."""
    thread = threading.Thread(target=run, args=(state_dir,), name="preload", daemon=True)
    thread.start()
    return thread
