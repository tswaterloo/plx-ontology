"""Partition hygiene: a held-out item must be genuinely unseen.

Amendment bb664f58910f section 2: held-out items are unseen, and related documents and
near-duplicates stay in one partition. An item is *related* to another when both read the
same source document or record; it is a *near-duplicate* when both name the same root or
the same identifier. Question templates are shared by design (the oracle generates every
family from one template), so wording alone is never a conflict; the identifiers in it are.

Keys of one item (``source_keys``):

* ``doc:`` every expected citation's document or record, version-free: an original by
  byte SHA-256 or SharePoint drive item, a mirror row by relation and key (any capture or
  generation), a mail message by id, any other handle without its fragment, query and
  ``@`` version; a 64-hex ``as_known_at`` pin (a byte pin of a curated original);
* ``root:`` the root identifier and the root row;
* ``id:`` identifier-like tokens of the question and its paraphrases (letters and digits,
  or five or more digits; years and short numbers are not identifiers).

``conflicts(held_out, visible)`` lists ``(held-out index, kind, visible item id)``; it
never returns a held-out value, so the runner can refuse with the kind alone.

**The live partition.** Items related through any key form one ``group`` (transitively: A
and B share a document, B and C a root). A group is never split: ``related(items, keys)``
names every item whose group touches a key of the other partition, so a run drops the
whole group, never one member. Keys cross partitions only as ``key_digests`` (SHA-256 of
each key, no value): the frozen held-out set keeps its digests in the credentialed host
state directory and the live dev run keeps its own there, so each side excludes what the
other holds without either ever reading the other's items.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

from evals import score as scoring

_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-#/._][A-Za-z0-9]+)*")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_YEAR = re.compile(r"^(?:19|20)[0-9]{2}$")
_DATE = re.compile(r"^[0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}$|^[0-9]{1,2}[-/.][0-9]{1,2}"
                   r"[-/.][0-9]{2,4}$")
KINDS = ("shared_source", "shared_root", "shared_identifier")


def _norm(text: Any) -> str:
    return re.sub(r"[^0-9a-z]", "", str(text or "").casefold())


def document_key(handle: Any) -> str | None:
    """The version-free document or record a citation handle names."""
    handle = str(handle or "")
    pinned = scoring.PINNED_ORIGINAL.match(handle)
    if pinned:
        return (f"sha256:{pinned.group(1)}" if pinned.group(1) else
                f"sharepoint:{unquote(pinned.group(2))}/{unquote(pinned.group(3))}")
    row = scoring.row_identity(handle, _oracle_tail(handle))
    if row is not None:
        return "row:" + scoring.canonical(list(row))
    scheme, sep, rest = handle.partition("://")
    if not sep or not rest:
        return None
    rest = re.split(r"[#?@]", rest, maxsplit=1)[0]
    if scheme == "graph-mail":  # graph-mail://<mailbox>/<message id>: the mailbox varies
        rest = unquote(rest.rsplit("/", 1)[-1])
    return f"{scheme}:{rest}" if rest else None


def _oracle_tail(handle: str) -> list[str]:
    match = scoring.ORACLE_HANDLE.match(handle)
    return [match.group(3)] if match else []


def _identifiers(text: Any) -> set[str]:
    """Identifier-like tokens; a short prefix word joins the number after it ("PO 5447")."""
    tokens = [token for token in _TOKEN.findall(str(text or "")) if not _DATE.match(token)]
    candidates = [_norm(token) for token in tokens]
    candidates += [_norm(a + b) for a, b in zip(tokens, tokens[1:])
                   if a.isalpha() and len(a) <= 4 and b.isdigit() and len(b) >= 4
                   and not _YEAR.match(b)]
    found = set()
    for key in candidates:
        digits = sum(ch.isdigit() for ch in key)
        if len(key) < 4 or not digits or _YEAR.match(key) or \
                (digits == len(key) and len(key) < 5):
            continue
        found.add(key)
    return found


def source_keys(item: Mapping[str, Any]) -> set[str]:
    keys = set()
    outcome = item.get("known_outcome") or {}
    for value in outcome.get("values") or []:
        for cite in value.get("citations") or []:
            doc = document_key(cite.get("l1_handle"))
            if doc:
                keys.add("doc:" + doc)
    for pin in ((item.get("as_known_at") or {}).get("l1_generations") or {}).values():
        if _HEX64.match(str(pin)):
            keys.add("doc:sha256:" + str(pin))
    root = item.get("root") or {}
    if _norm(root.get("identifier")):
        keys.add("root:" + _norm(root.get("identifier")))
    if root.get("relation") and root.get("key"):
        keys.add("root:row:" + scoring.canonical([root["relation"], root["key"]]))
    question = item.get("question") or {}
    for text in [question.get("text"), root.get("identifier"),
                 *(question.get("paraphrases") or [])]:
        keys |= {"id:" + token for token in _identifiers(text)}
    return keys


def _kind(key: str) -> str:
    return {"doc": "shared_source", "root": "shared_root"}.get(key.split(":", 1)[0],
                                                              "shared_identifier")


def conflicts(held_out: Sequence[Mapping[str, Any]], visible: Iterable[Mapping[str, Any]]
              ) -> list[tuple[int, str, str]]:
    """Every (held-out index, kind, visible item id) whose keys meet; sorted, deduplicated."""
    owners: dict[str, set[str]] = {}
    for item in visible:
        for key in source_keys(item):
            owners.setdefault(key, set()).add(str(item.get("id")))
    found = set()
    for index, item in enumerate(held_out):
        for key in source_keys(item):
            for owner in owners.get(key, ()):
                found.add((index, _kind(key), owner))
    return sorted(found)


def key_digests(item: Mapping[str, Any]) -> set[str]:
    """``source_keys`` as SHA-256 digests: what may cross into the other partition."""
    return {hashlib.sha256(key.encode("utf-8")).hexdigest() for key in source_keys(item)}


def groups(items: Sequence[Mapping[str, Any]]) -> list[list[int]]:
    """Indices of ``items`` joined by shared keys (union-find), each group sorted."""
    parent = list(range(len(items)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owner: dict[str, int] = {}
    for index, item in enumerate(items):
        for key in source_keys(item):
            if key in owner:
                parent[root(index)] = root(owner[key])
            else:
                owner[key] = index
    found: dict[int, list[int]] = {}
    for index in range(len(items)):
        found.setdefault(root(index), []).append(index)
    return sorted(found.values())


def related(items: Sequence[Mapping[str, Any]], other: Iterable[str]) -> set[int]:
    """Indices of every item whose group touches one of ``other`` (key digests of the
    other partition): the whole group goes with it."""
    other = set(other)
    touched = {index for index, item in enumerate(items) if key_digests(item) & other}
    return {index for group in groups(items) if touched & set(group) for index in group}


__all__ = ["KINDS", "conflicts", "document_key", "groups", "key_digests", "related",
           "source_keys"]
