"""Composition lines: moved down to ``ontology.composition_lines`` (the build step
``with_formula_lines`` reads the same sheets; the layer contract keeps ``ontology`` from
importing ``operating``). This name stays for the readers above it."""

from ontology.composition_lines import CHECKED, FORMAT, SHARE_TOTAL_TOLERANCE, read_sheet

__all__ = ["CHECKED", "FORMAT", "SHARE_TOTAL_TOLERANCE", "read_sheet"]
