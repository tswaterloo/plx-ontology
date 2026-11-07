"""The per-item feedback signal the AI watcher and the proactive engine read.

People mark a briefing or alert item not useful or wrong in the app; the app's feedback store
(``operator_api.feedback``) writes ``<state>/feedback/signals.json``: per item id, a signal
for every reader (``global``) and per reader (their reader key: sorted principals). Each is a
verdict and a reason. An item a signal covers is suppressed: left out of that reader's
briefing (or every reader's), with the reason kept in the feedback store. No rule is per
company, supplier or record: the signal is only about the one item someone marked.

``PLX_FEEDBACK_DIR`` overrides the directory, as it does for the store. A missing or
unreadable file suppresses nothing (feedback never hides an item by accident).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

FILE = "signals.json"
_CACHE: dict[str, tuple[tuple, dict[str, Any]]] = {}
_LOCK = threading.Lock()


def state_of(store: Any) -> Optional[Path]:
    """The state directory a served store sits in (``<state>/projection/<file>`` or a
    generation's ``<state>/projection/generations/<g>/<file>``)."""
    if store is None:
        return None
    store = Path(store)
    parent = store.resolve().parent if store.exists() else store.parent
    if parent.name == "projection":
        return parent.parent
    if parent.parent.name == "generations":
        return parent.parent.parent.parent
    return None


def signals_path(state_dir: Optional[Path]) -> Optional[Path]:
    override = os.environ.get("PLX_FEEDBACK_DIR")
    if override:
        return Path(override) / FILE
    return Path(state_dir) / "feedback" / FILE if state_dir is not None else None


def signals(state_dir: Optional[Path]) -> dict[str, Any]:
    """Item id -> {"global": signal | None, "readers": {reader key: signal}} (cached by the
    file's change time and size)."""
    path = signals_path(state_dir)
    if path is None:
        return {}
    try:
        found = os.stat(path)
    except OSError:
        return {}
    stamp = (found.st_mtime_ns, found.st_size)
    with _LOCK:
        cached = _CACHE.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
    try:
        items = json.loads(path.read_text("utf-8")).get("items") or {}
        items = items if isinstance(items, dict) else {}
    except (OSError, ValueError, AttributeError):
        items = {}
    with _LOCK:
        _CACHE[str(path)] = (stamp, items)
    return items


def suppressor(state_dir: Optional[Path]) -> Callable[[str, Optional[str]], Optional[str]]:
    """``hide(item_id, reader)``: the reason the item is suppressed for that reader (None:
    shown). ``reader`` None asks only for the signal every reader shares."""
    items = signals(state_dir)
    if not items:
        return lambda item_id, reader=None: None

    def hide(item_id: str, reader: Optional[str] = None) -> Optional[str]:
        entry = items.get(str(item_id))
        if not isinstance(entry, dict):
            return None
        shared = entry.get("global")
        if isinstance(shared, dict):
            return str(shared.get("reason") or "Marked by feedback")
        if reader:
            for key, signal in (entry.get("readers") or {}).items():
                if isinstance(signal, dict) and (reader == key or reader.endswith(":" + key)):
                    return str(signal.get("reason") or "Marked by feedback")
        return None

    return hide


def stamp(state_dir: Optional[Path]) -> Optional[tuple]:
    """The signal file's change time and size, for a page cache's key."""
    path = signals_path(state_dir)
    try:
        found = os.stat(path) if path is not None else None
    except OSError:
        return None
    return (found.st_mtime_ns, found.st_size) if found else None
