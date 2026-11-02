"""Short handle aliases for one agent session: the agent reads and writes ``@KQT7`` where a
tool result named a long citation handle (``original://...#page=3``), so it writes fewer
output tokens; the host keeps the mapping and expands every alias back to the full handle
before any tool, the answer gate or the final screen reads it.

* An alias is minted only for a whole string a tool result returned that is an L1 handle
  (``answer_gate.HANDLE``); a handle written inside a text is left as it is.
* Each session has its own random tag (``@`` + 3 letters): an alias names a handle only in
  the session that minted it. A string shaped like an alias that this session never
  minted (another session's, a guess) is refused (``unknown_aliases``), never mapped.
* Expansion is exact: the alias, optionally followed by a page fragment (``#page=2``,
  ``&page=2``), becomes the handle with that fragment; the host still decides the handle
  (permissions, evidence of the session) as it decides one written in full.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Mapping

ALIAS_PREFIX = "@"
TAG_LETTERS = "BCDFGHJKLMNPQRSTVWXZ"   # no vowels: a tag never spells a word
# Any alias-shaped token, this session's or not: ``@`` + 3 tag letters + a number, not
# glued to a word or an address before it.
ALIAS_TOKEN = re.compile(r"(?<![\w@.])@([A-Z]{3})([1-9][0-9]{0,5})(?![\w@])")
MAX_ALIASES = 20_000
UNKNOWN_ALIAS = "unknown_handle_alias"


class Aliases:
    """One session's handle aliases (thread-safe enough for the gate's relay: minting is
    a dict insert under the GIL)."""

    def __init__(self, tag: str | None = None) -> None:
        self.tag = tag or "".join(secrets.choice(TAG_LETTERS) for _ in range(3))
        self._by_handle: dict[str, str] = {}
        self._by_alias: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._by_alias)

    def alias(self, handle: str) -> str:
        """The alias of ``handle``, minted on first sight (the handle itself past
        ``MAX_ALIASES``)."""
        known = self._by_handle.get(handle)
        if known is not None:
            return known
        if len(self._by_alias) >= MAX_ALIASES:
            return handle
        alias = f"{ALIAS_PREFIX}{self.tag}{len(self._by_alias) + 1}"
        self._by_handle[handle], self._by_alias[alias] = alias, handle
        return alias

    # ---- outbound: a tool result as the agent reads it -----------------------------

    def shorten(self, value: Any) -> Any:
        """``value`` with every whole-string L1 handle replaced by its alias."""
        from operating.answer_gate import HANDLE
        if isinstance(value, str):
            return self.alias(value) if HANDLE.match(value) else value
        if isinstance(value, Mapping):
            return {key: self.shorten(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.shorten(item) for item in value]
        return value

    # ---- inbound: what the agent wrote, as the host reads it -----------------------

    def _expand_text(self, text: str, unknown: list[str]) -> str:
        def one(match: re.Match) -> str:
            alias = match.group(0)
            handle = self._by_alias.get(alias)
            if handle is None:
                unknown.append(alias)
                return alias
            return handle
        return ALIAS_TOKEN.sub(one, text)

    def expand(self, value: Any, unknown: list[str] | None = None) -> Any:
        """``value`` with every alias this session minted replaced by its handle, in any
        string (a citation, a claim's text); each alias-shaped token it never minted is
        appended to ``unknown`` and left as written (the caller refuses)."""
        unknown = [] if unknown is None else unknown
        if isinstance(value, str):
            return self._expand_text(value, unknown) if ALIAS_PREFIX in value else value
        if isinstance(value, Mapping):
            return {key: self.expand(item, unknown) for key, item in value.items()}
        if isinstance(value, list):
            return [self.expand(item, unknown) for item in value]
        return value

    def unknown_aliases(self, value: Any) -> list[str]:
        """The alias-shaped tokens in ``value`` this session never minted."""
        unknown: list[str] = []
        self.expand(value, unknown)
        return list(dict.fromkeys(unknown))

    def restore(self, value: Any) -> Any:
        """A tool result the agent was shown, back as the host returned it: each
        whole-string alias this session minted is its handle again (text is left as
        written: a source's own words are never rewritten)."""
        if isinstance(value, str):
            return self._by_alias.get(value, value)
        if isinstance(value, Mapping):
            return {key: self.restore(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.restore(item) for item in value]
        return value


def shorten_result(aliases: Aliases, result: Any) -> Any:
    """An MCP ``tools/call`` result as the agent reads it: its ``structuredContent`` and
    each JSON text part with handles aliased (a text part that is no JSON stays as is).
    An error result is aliased too (it may name a handle the agent sent)."""
    import json
    if not isinstance(result, dict):
        return result
    out = dict(result)
    if isinstance(out.get("structuredContent"), (dict, list)):
        out["structuredContent"] = aliases.shorten(out["structuredContent"])
    content = out.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" \
                    and isinstance(part.get("text"), str):
                try:
                    parsed = json.loads(part["text"])
                except ValueError:
                    parts.append(part)
                    continue
                part = {**part, "text": json.dumps(aliases.shorten(parsed),
                                                   ensure_ascii=False)}
            parts.append(part)
        out["content"] = parts
    return out
