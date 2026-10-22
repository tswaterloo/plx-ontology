"""Shared identity and permission value types (C0 contract).

These are the only ontology names most of the Workbench needs: who is reading
(`Actor`), which source record a fact is about (`EntityRef`), the clearance
levels and the validation error. They are free of SQLite.

`ontology.core` re-exports every name for existing importers. New code imports
from `ontology.types` (or `ontology`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote


class ValidationError(ValueError):
    pass


LEVELS = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a nonempty string")
    return value.strip()


def _acl(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValidationError("ACL must be an iterable of principal names")
    return tuple(sorted({_required(value, "principal") for value in values}))


@dataclass(frozen=True, order=True)
class EntityRef:
    source: str
    entity_type: str
    native_id: str

    def __post_init__(self) -> None:
        for key in ("source", "entity_type", "native_id"):
            object.__setattr__(self, key, _required(getattr(self, key), key))

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "entity_type": self.entity_type, "native_id": self.native_id}

    @property
    def entity_id(self) -> str:
        """Stable, source-qualified identity; no cross-system merge is implied."""
        return "::".join(quote(part, safe="") for part in
                         (self.source, self.entity_type, self.native_id))


@dataclass(frozen=True)
class Actor:
    tenant: str
    principals: frozenset[str]
    clearance: str = "internal"

    def __post_init__(self) -> None:
        _required(self.tenant, "tenant")
        _acl(self.principals)
        if self.clearance not in LEVELS:
            raise ValidationError("unknown clearance")


__all__ = ["Actor", "EntityRef", "LEVELS", "ValidationError"]
