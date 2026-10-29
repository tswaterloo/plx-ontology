"""PLX ontology package.

Importing the package loads only the shared value types (`ontology.types`). The legacy
SQLite store and identity resolver are deleted; gate check `no-legacy-ontology-imports`
(`scripts/check_legacy_ontology_imports.py`) keeps their names from coming back.
"""

from .types import LEVELS, Actor, EntityRef, ValidationError

__all__ = ["Actor", "EntityRef", "LEVELS", "ValidationError"]
