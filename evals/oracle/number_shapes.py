"""Measurement for gap-b01a86b2f438: do Portal sales-order numbers reach BC at all?

Counts and shape classes only, never a value. A shape class writes each run of one
character class as ``U`` (upper case), ``L`` (lower), ``D`` (digit), ``_`` (space) with its
run length, any non-ASCII character as ``X`` (with its run length), the separators in
``SEPARATORS`` as themselves and any other character as ``?``: ``SO-2026-0042`` is
``U2-D4-D4``. A shape names no identifier, but short ids share shapes, so a class seen fewer than ``MIN_CLASS`` times
is pooled as ``other``. Matches are exact on the trimmed text, as ``BcReads.find`` compares.

For the sampled Portal sales orders, per Portal field that could carry the number, and per
BC field a declared edge (``keys.EDGES``, ``keys.REFERENCE_EDGES``) or the Portal's BC
sync maps it to: the shape counts; how many Portal values appear in the BC sample read
(``in_bc_sample``); and, for the first ``probes`` roots, how many exact BC lookups held a
record (``probe_found``), several (``probe_shared``) or failed (``probe_failed``); with a
``remaining`` clock, probing stops once it reads zero (``probes_stopped``; ``probed``
counts the lookups made). The BC sample reads run before it. Read-only.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from . import keys as K
from .families import Sources
from .source import ReadFailed, canonical, field_value, key_text, sql_trim

PORTAL_FIELDS = ("order_number", "po_reference", "custom_fields.fmOeNumber",
                 "custom_fields.erpCode", "custom_fields.sageOrdNumber",
                 "custom_fields.custPoCode", "custom_fields.renumberedTo")
# BC fields the Portal's BC sync writes or reads back (03 §3.2/3.3), beside the
# fields the declared edges name.
BC_FIELDS = (("salesOrders", "number"), ("salesOrders", "externalDocumentNumber"),
             ("salesInvoices", "externalDocumentNumber"), ("salesInvoices", "orderNumber"))
BC_SAMPLE_LIMIT = 500
MIN_CLASS = 3
MAX_RUNS = 12
SEPARATORS = frozenset("-/._#")


def shape(text: str) -> str:
    runs: list[list[Any]] = []
    for ch in text:
        cls = ("X" if not ch.isascii() else "U" if ch.isupper() else "L" if ch.islower()
               else "D" if ch.isdigit() else "_" if ch.isspace()
               else ch if ch in SEPARATORS else "?")
        if runs and runs[-1][0] == cls and cls in "ULDX_":
            runs[-1][1] += 1
        else:
            runs.append([cls, 1])
    out = "".join(f"{c}{n}" if c in "ULDX_" else c for c, n in runs[:MAX_RUNS])
    return out + ("+" if len(runs) > MAX_RUNS else "")


def _shapes(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[shape(value)] = counts.get(shape(value), 0) + 1
    pooled = {k: v for k, v in counts.items() if v >= MIN_CLASS}
    rest = sum(v for k, v in counts.items() if v < MIN_CLASS)
    return {**dict(sorted(pooled.items())), **({"other": rest} if rest else {})}


def bc_fields() -> list[tuple[str, str]]:
    named = set(BC_FIELDS)
    for edge in K.EDGES + K.REFERENCE_EDGES:
        for relation, column in ((edge.source, edge.source_field),
                                 (edge.target, edge.target_field)):
            if relation in ("bc:salesOrders", "bc:salesInvoices"):
                named.add((relation[3:], column))
    return sorted(named)


def _text(value: Any) -> str | None:
    text = key_text(value)
    return sql_trim(text) if text is not None and sql_trim(text) else None


def measure(sources: Sources, probes: int = 20, sample_limit: int = BC_SAMPLE_LIMIT,
            remaining: Callable[[], float] | None = None) -> dict[str, Any]:
    from .cross_sample import portal_roots
    roots = sorted(portal_roots(sources, "sales_order"),
                   key=lambda r: (r.relation, canonical(r.key)))
    rows = [r.node.row for r in roots]
    targets = bc_fields()
    held: dict[tuple[str, str], list[str]] = {}
    bc_state: dict[str, str] = {}
    if sources.bc is None:
        bc_state = {"all": "not_configured"}
    else:
        for entity_set in sorted({e for e, _ in targets}):
            try:
                records = sources.bc.sample(entity_set, sample_limit)
            except ReadFailed as exc:
                bc_state[entity_set] = "read_failed"
                continue
            for _, column in (t for t in targets if t[0] == entity_set):
                held[(entity_set, column)] = [
                    t for t in (_text(r["fields"].get(column)) for r in records) if t]
    out: dict[str, Any] = {
        "portal_roots": len(roots), "probe_roots": min(probes, len(roots)),
        "bc_read": bc_state or "read",
        "bc": {f"{e}.{c}": {"sampled": len(v), "shapes": _shapes(v)}
               for (e, c), v in sorted(held.items())},
        "portal": {}}
    for name in PORTAL_FIELDS:
        values = [t for t in (_text(field_value(row, name)) for row in rows) if t]
        entry: dict[str, Any] = {"present": len(values), "shapes": _shapes(values),
                                 "distinct": len(set(values)), "against": {}}
        for (entity_set, column), bc_values in sorted(held.items()):
            bag = set(bc_values)
            entry["against"][f"{entity_set}.{column}"] = {
                "in_bc_sample": sum(1 for v in values if v in bag)}
        probed = [t for t in (_text(field_value(r, name)) for r in rows[:probes]) if t]
        if sources.bc is not None:
            for entity_set, column in targets:
                tried = found = shared = failed = 0
                for value in probed:
                    if remaining is not None and remaining() <= 0:
                        out["probes_stopped"] = "run_budget_exhausted"
                        break
                    tried += 1
                    try:
                        records, _ = sources.bc.find(entity_set, column, value)
                    except ReadFailed:
                        failed += 1
                        continue
                    found += bool(records)
                    shared += len(records) > 1
                cell = entry["against"].setdefault(f"{entity_set}.{column}", {})
                cell.update(probed=tried, probe_found=found, probe_shared=shared,
                            probe_failed=failed)
        out["portal"][name] = entry
    return out
