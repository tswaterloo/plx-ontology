"""Source-independent ontology generation: source facts -> types, identity keys, links
and markings (``build.py``), with the semantics a source does not state read from the
declarative overrides in ``ontology/schema/overrides/`` (``overrides.py``). A source
adapter (``scripts/gen_ontology_skeleton.py`` for the Portal) only reads its source."""

from ontology.generator.build import binding_id, generate, pascal, property_name, snake, type_id
from ontology.generator.overrides import OVERRIDES_DIR, NameRule, OverrideError, Overrides, \
    load_overrides

__all__ = ["OVERRIDES_DIR", "NameRule", "OverrideError", "Overrides", "binding_id", "generate",
           "load_overrides", "pascal", "property_name", "snake", "type_id"]
