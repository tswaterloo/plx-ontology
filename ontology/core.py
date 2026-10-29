"""Compatibility re-export of the shared value types (``ontology.types``).

The legacy SQLite operational store and its identity resolver are deleted (lane
identity-ledger-retire): human identity
decisions live in the one append-only review store (``ontology.reviews``) and the
projection's mention links and case mail read it (``operating.case_mail``). Existing
importers keep ``from ontology.core import Actor``; new code imports ``ontology.types``.
"""

from __future__ import annotations

from .types import LEVELS, Actor, EntityRef, ValidationError  # noqa: F401  (re-exported)

__all__ = ["Actor", "EntityRef", "LEVELS", "ValidationError"]
