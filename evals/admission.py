"""One admission rule for proposed eval units.

A proposed unit enters a scored set only through the existing partitions (evals/dev,
evals/exposed) and only when ``admissible`` returns no reasons."""
from __future__ import annotations

import re
from typing import Any, Mapping

from evals.oracle.generator import GOLD_LINEAGE_PREFIX, LINEAGE_PREFIXES, ORACLE_ID_PREFIXES
from evals.oracle.keys import RELATIONS as PORTAL_RELATIONS
from evals.score import PINNED_SOURCES, partition_of, uncited_expected

# Generated-ness is oracle provenance: a dev-set item carries a family oracle's id
# (the generator's own list, never a copy). Its lineage, one of the generator's strata,
# names real data and proves nothing alone.
GENERATED_ORACLE_IDS = ORACLE_ID_PREFIXES
SYNTHETIC_LINEAGE_PREFIXES = LINEAGE_PREFIXES
# A derivation naming a source system or record (``bc:production``, ``api://...``), not a
# rule cite.
_SOURCE_REF = re.compile(r"^[a-z][a-z0-9+.-]*:")


def reads_only_portal(item: Mapping[str, Any]) -> bool:
    """Whether every source an item's label reads is a Portal relation: its root, each
    chain record and each citation (``api://``); a BC document or a mail or SharePoint
    original is another source."""
    outcome = item.get("known_outcome") or {}
    relations = [(item.get("root") or {}).get("relation"),
                 *(entry.get("relation") for entry in outcome.get("chain") or [])]
    handles = [str(cite.get("l1_handle", "")) for value in outcome.get("values") or []
               for cite in value.get("citations") or []]
    return all(r in PORTAL_RELATIONS for r in relations) and \
        all(h.startswith("api://") for h in handles)


def admissible(item: Mapping[str, Any], *, perturbed_portal: bool = False) -> list[str]:
    """Reason codes that keep ``item`` out of a scored set; empty when it may be admitted.
    ``perturbed_portal``: the caller's host serves the Portal perturbed as a metamorphic
    variant records (``scripts/eval_dev_baseline.py heldout``), so a variant whose label
    reads only the Portal (``reads_only_portal``) is admitted; no host perturbs BC or a
    document."""
    reasons: list[str] = []
    outcome = item.get("known_outcome") or {}
    oracle = item.get("oracle") or {}
    derived = [str(ref) for ref in oracle.get("derived_from") or []]
    if not (outcome.get("values") or outcome.get("gaps")):
        # score.py passes any answer with matching answerability when no value is expected.
        reasons.append("empty_outcome")
    exposed = partition_of(item) == "exposed"
    # Exposed cases derive from pinned originals, not from an oracle's closed root.
    if not exposed and item.get("closed_transaction") is not True:
        reasons.append("open_transaction")
    generated = str(oracle.get("id", "")).startswith(GENERATED_ORACLE_IDS)
    lineage = str(item.get("lineage_stratum", ""))
    if exposed:
        # A real case roots at its pinned original or source-of-record row, never at a
        # Portal gold copy the generator samples, and no family oracle answered it.
        mismatch = generated or lineage.startswith(GOLD_LINEAGE_PREFIX)
    else:
        mismatch = not generated or not lineage.startswith(SYNTHETIC_LINEAGE_PREFIXES)
    if mismatch:
        reasons.append("lineage_partition_mismatch")
    if not exposed and any(_SOURCE_REF.match(ref) for ref in derived):
        reasons.append("live_source_derivation")  # real data belongs in -exposed
    if exposed and outcome.get("values") and (
            not any(ref.startswith(PINNED_SOURCES) for ref in derived)
            or uncited_expected(item)):
        reasons.append("uncited_values")
    if item.get("perturbation") is not None and not (perturbed_portal and
                                                      reads_only_portal(item)):
        # A metamorphic variant's outcome is the oracle's over perturbed reads
        # (evals/oracle/perturb.py); a host that does not serve them all would be scored
        # against the unperturbed source.
        reasons.append("perturbed_reads_not_served")
    return reasons
