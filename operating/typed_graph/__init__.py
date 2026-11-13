"""Typed objects over the served graph (``runtime``) and the generated models
(``models``, from the ontology schema by ``tools/gen_models.py``). docs/data-model.md."""
from operating.typed_graph.runtime import (  # noqa: F401
    CardinalityError, Cited, Citation, GraphModel, GraphReadError, GraphRepository, LinkSpec,
    Money, PropertySpec, Quantity, Related, SchemaMismatch)
