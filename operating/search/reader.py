"""Who is searching (the permission scope every SQL query carries) and what was asked
(or a bare question).

The scope is the existing per-reader ``ReadPolicy`` decision, not a new one: the
audience units this reader may read (``policy.may_read`` over every unit the search
tables hold) and the markings they hold (``policy.held_markings``). Default deny: a
reader with no policy reads nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from operating.search.config import INTENTS

# Citation schemes whose items carry their own ACL (mail, files, chats): a shown hit of
# one passes the item gate, and without a gate it is never shown (ontology_tools).
ITEM_ACL_SCHEMES = frozenset({"graph-mail", "sharepoint", "original", "teams-channel",
                              "teams-chat", "sharepoint-list"})


@dataclass(frozen=True)
class SearchReader:
    """``label``: who is logged as asking. ``may_read(audience_key)``: the policy's
    decision for one unit. ``held``: the markings held. ``item_gate(handle)``: the
    per-item re-check of a hit about to be shown (None: items of ``ITEM_ACL_SCHEMES``
    are never shown)."""
    label: str
    may_read: Callable[[str], bool]
    held: frozenset[str]
    item_gate: Callable[[str], bool] | None = None

    @classmethod
    def from_policy(cls, policy: Any, actor: Any, *,
                    item_gate: Callable[[Any, str], bool] | None = None,
                    label: str | None = None) -> "SearchReader":
        principals = sorted(getattr(actor, "principals", ()) or ())
        return cls(
            label=label or (principals[0] if principals else "unknown"),
            may_read=lambda key: bool(policy.may_read(actor, key)),
            held=frozenset(policy.held_markings(actor)),
            item_gate=(None if item_gate is None
                       else (lambda handle: item_gate(actor, handle) is True)))

    def allowed_keys(self, keys: Sequence[str]) -> list[str]:
        out = []
        for key in keys:
            try:
                if self.may_read(key) is True:
                    out.append(key)
            except Exception:  # noqa: BLE001 - a failing decision denies
                continue
        return out

    def item_allowed(self, handle: str) -> bool:
        scheme = handle.split("://", 1)[0] if isinstance(handle, str) else ""
        if scheme not in ITEM_ACL_SCHEMES:
            return True
        if self.item_gate is None:
            return False
        try:
            return self.item_gate(handle) is True
        except Exception:  # noqa: BLE001 - a failing re-check denies
            return False


@dataclass(frozen=True)
class Entity:
    text: str
    resolved: str | None
    candidates: tuple[tuple[str, float], ...] = ()   # (object_id, score)
    ambiguous: bool = False


@dataclass(frozen=True)
class Query:
    """What ``search`` reads from an Understanding."""
    question: str
    intent: str
    entities: tuple[Entity, ...] = ()
    rewrites: tuple[str, ...] = ()
    time_frame: Any = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def resolved_ids(self) -> list[str]:
        return list(dict.fromkeys(e.resolved for e in self.entities if e.resolved))

    def candidate_ids(self) -> list[tuple[str, float]]:
        """Candidates of unresolved (ambiguous) entities: graph leads only, never exact."""
        out: dict[str, float] = {}
        for entity in self.entities:
            if entity.resolved:
                continue
            for object_id, score in entity.candidates:
                out[object_id] = max(out.get(object_id, 0.0), score)
        return sorted(out.items(), key=lambda kv: -kv[1])[:10]

    def id_tokens(self) -> list[str]:
        """Identifier-shaped words of the question and the entity texts (a digit in
        them): matched against the ids a chunk names (``linked_ids``)."""
        texts = [self.question] + [e.text for e in self.entities]
        out: list[str] = []
        for text in texts:
            for token in re.findall(r"[A-Za-z]*[-_/]?\d[\w\-/.]*", text or ""):
                token = token.strip(".-/")
                if len(token) >= 3 and token not in out:
                    out.append(token)
        return out[:12]

    def texts(self) -> list[str]:
        """The question and its rewrites (multi-query), deduplicated, at most 6."""
        out = [self.question]
        for rewrite in self.rewrites:
            if isinstance(rewrite, str) and rewrite.strip() and rewrite not in out:
                out.append(rewrite)
        return out[:6]


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def as_query(understanding: Any, question: str | None = None) -> Query:
    """A ``Query`` from the Understanding (object or dict) or a bare question
    string. An intent outside the contract's list reads as ``other``."""
    if isinstance(understanding, str):
        return Query(understanding.strip(), "other")
    if understanding is None:
        raise ValueError("search_understanding_required")
    text = question or _get(understanding, "question") or _get(understanding, "text") or ""
    intent = _get(understanding, "intent")
    intent = intent if intent in INTENTS else "other"
    entities = []
    for item in _get(understanding, "entities", None) or ():
        candidates = []
        for cand in _get(item, "candidates", None) or ():
            oid, score = _get(cand, "object_id"), _get(cand, "score", 0.0)
            if isinstance(oid, str) and oid:
                try:
                    candidates.append((oid, float(score or 0.0)))
                except (TypeError, ValueError):
                    candidates.append((oid, 0.0))
        resolved = _get(item, "resolved")
        entities.append(Entity(str(_get(item, "text", "") or ""),
                               resolved if isinstance(resolved, str) and resolved else None,
                               tuple(candidates), bool(_get(item, "ambiguous", False))))
    rewrites = tuple(r for r in (_get(understanding, "rewrites", None) or ())
                     if isinstance(r, str))
    if not isinstance(text, str) or not text.strip():
        text = rewrites[0] if rewrites else ""
    return Query(text.strip(), intent, tuple(entities), rewrites,
                 _get(understanding, "time_frame"),
                 understanding if isinstance(understanding, Mapping) else {})
