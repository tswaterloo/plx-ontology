"""Short references (``ref``) to the long handles ``plx.open`` reads, so a model opening a
mail, Teams message, meeting or document writes a dozen characters instead of the 100-230
of its handle (a mail handle is its mailbox id and its Graph message id: about 70 output
tokens, ~1.5 s of model writing per open).

A ``plx.search`` hit and a ``plx.sql`` row whose ``handle`` column holds such a handle get a
``ref`` beside it (the full handle stays where it was: the app's cited tables, Assist and
every other reader of the result are unchanged). ``plx.open`` given a ref reads the full
handle this session's own results showed for it, then opens that exactly as before: the
gates decide on the full handle for the session's reader, so a ref never opens anything
its full handle would not. A ref no result of this session showed is refused (fail
closed), never guessed.

The form is ``<scheme>://r.<10 base32 letters of the handle's sha256>``: it matches the
handle grammar of the contracts (``$defs/handle``) and the firewall's token grammar, so it
leaves the output boundary bare."""
from __future__ import annotations

import base64
import hashlib
import re
from collections import OrderedDict
from typing import Any, Mapping

# the documents plx.open is for (its contract: mail, Teams, meetings, files, originals)
SCHEMES = ("graph-mail", "teams-chat", "teams-channel", "calendar-event",
           "meeting-transcript", "sharepoint", "original")
MIN_CHARS = 48       # a shorter handle is written as it is
MOST = 20000         # refs a session keeps (the oldest go first)
COLUMN = "handle"    # the plx.sql column whose handles get a ref
_SHORT = re.compile(r"^([a-z][a-z-]{1,30})://r\.([a-z2-7]{10})$")


_IN_TEXT = re.compile(r"(?:%s)://[A-Za-z0-9_.:/@+=!,#%%?&~-]+" % "|".join(SCHEMES))


def learn(text: str, into: dict[str, str], most: int = MOST) -> None:
    """Every ref ``text`` (a relayed tool result) shows with the full handle it shows it
    for, into ``into`` (ref -> handle). The session's gate (``agent_session.serve_gate``)
    learns them so a ref opens on whichever of the session's hosts serves the open (a
    helper host keeps its own ``Refs``)."""
    if "://r." not in text:
        return
    found = set(_IN_TEXT.findall(text))
    for handle in found:
        if len(handle) >= MIN_CHARS and len(into) < most:
            ref = short(handle)
            if ref in found:
                into.setdefault(ref, handle)


def expand(arguments: Any, refs: Mapping[str, str]) -> Any:
    """``plx.open``'s arguments with a learnt ref as its full handle (else as they are)."""
    if isinstance(arguments, Mapping) and is_ref(arguments.get("citation")) \
            and arguments["citation"] in refs:
        arguments = {**arguments, "citation": refs[arguments["citation"]]}
    many = arguments.get("citations") if isinstance(arguments, Mapping) else None
    if isinstance(many, list) and any(is_ref(c) and c in refs for c in many):
        arguments = {**arguments, "citations": [refs[c] if is_ref(c) and c in refs else c
                                                for c in many]}
    return arguments


def short(handle: str) -> str:
    """The ref of ``handle`` (deterministic)."""
    scheme = handle.split("://", 1)[0]
    code = base64.b32encode(hashlib.sha256(handle.encode("utf-8")).digest())
    return f"{scheme}://r.{code.decode('ascii').lower()[:10]}"


def is_ref(citation: Any) -> bool:
    return isinstance(citation, str) and _SHORT.match(citation) is not None


class Refs:
    """One session's refs: ref -> the full handle a result of this session showed."""

    def __init__(self, most: int = MOST) -> None:
        self._full: OrderedDict[str, str] = OrderedDict()
        self.most = most

    def ref(self, handle: Any) -> str | None:
        """The ref to show beside ``handle``, kept for ``resolve``; None for a handle that
        is written as it is (short, not a document's, or its ref already names another
        handle of this session: then only the full handle is shown)."""
        if not isinstance(handle, str) or len(handle) < MIN_CHARS or \
                handle.split("://", 1)[0] not in SCHEMES or "://" not in handle:
            return None
        ref = short(handle)
        if self._full.get(ref, handle) != handle:
            return None
        self._full[ref] = handle
        self._full.move_to_end(ref)
        while len(self._full) > self.most:
            self._full.popitem(last=False)
        return ref

    def resolve(self, citation: Any) -> Any:
        """``citation`` itself, or the full handle of a ref this session showed; None for a
        ref it did not show."""
        return self._full.get(citation) if is_ref(citation) else citation

    def search(self, result: Any) -> Any:
        """A ``plx.search`` result with a ref beside each hit's long citation."""
        if isinstance(result, dict) and isinstance(result.get("documents"), list):
            for hit in result["documents"]:
                if isinstance(hit, dict):
                    ref = self.ref(hit.get("citation"))
                    if ref is not None:
                        hit["ref"] = ref
        return result

    def sql(self, result: Any) -> Any:
        """A ``plx.sql`` result with a ref on each row whose ``handle`` cell is long."""
        if not (isinstance(result, dict) and isinstance(result.get("columns"), list)
                and isinstance(result.get("rows"), list)):
            return result
        try:
            at = result["columns"].index(COLUMN)
        except ValueError:
            return result
        for row in result["rows"]:
            cells = row.get("cells") if isinstance(row, Mapping) else None
            if isinstance(row, dict) and isinstance(cells, list) and at < len(cells):
                ref = self.ref(cells[at])
                if ref is not None:
                    row["ref"] = ref
        return result


__all__ = ["COLUMN", "MIN_CHARS", "MOST", "Refs", "SCHEMES", "expand", "is_ref", "learn", "short"]
