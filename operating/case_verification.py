"""Read-back verification of a case proposal's expected effect (never dispatch).

The case runtime records proposals; approval and execution belong to Portal and
Mission Control (devloop/UPSTREAM-MAP.md: the Workbench keeps only read-back
verification observations). This module is that read-back: for a proposal that
declared an ``expected_effect`` on one cited source row, re-read that row for
the calling actor through the audience-checked source mirror (refreshed when it
is older than ``max_staleness``) and record on the case what the source shows:

* ``effect_observed``: a listed field of the current row holds the expected
  value. It does not establish who changed the row, whether anyone approved it,
  or that the proposal caused it (``attribution: not_established``);
  ``held_when_proposed`` says whether the cited row already held it.
* ``effect_not_observed``: every listed field was read and none holds it. It is
  not proof that nothing was done: the replica may lag and the write may have
  gone elsewhere.
* ``unknown`` with a reason: the relation could not be refreshed or is served from
  a persisted capture (a copy never verifies an effect), the row is no
  longer returned or its key is not unique, a field could not be read, or the
  row's citation no longer resolves for this actor.

A field that holds another value is reported as ``differs`` and its value is
never echoed (it can be a person's name). The verification cites the row read
back and every citation of the proposal, so a case reader who cannot resolve
all of them sees it withheld. Nothing here writes to a business system.

``check_claims`` is the case-level semantic check (``operating.claim_checks``):
every structured value claim of every finding and gap the actor can read is
checked against the field it cites, re-read now for that actor
(``CaseRuntime.claim_row``, the claim gate's reader) and digest-checked against
what the case recorded (value, role contract, unit; a mirror revision a later
capture superseded is ``claim_changed_since``, never passed), and
every number in their text and in the close outcome must be covered by a claim.
A case passes only when nothing fails and no finding or gap is withheld or
changed; it never writes to the case. ``verify_proposal`` also returns the claim
checks of the proposal's evidence finding (``evidence_claims``), so a read-back of a
proposal resting on a role-mismatched claim says so.
"""

from __future__ import annotations

from decimal import Decimal
import math
from typing import Any, Mapping, Sequence

from ontology.core import Actor, ValidationError

from .capability_guards import _number, number_state, read_field, value_state
from .cases import _digest as _citation_digest
from .cases import effect_outcome, effect_values
from .claim_checks import case_items, check_items

SCHEMA = "plx-case-verification/v1"
_MAX_REFERENCES = 10
_MAX_REFERENCE_CHARS = 200
NOTE = ("Read-back only: the Workbench did not approve, dispatch or execute anything. "
        "effect_observed says what the source row shows now, not who changed it or why; "
        "effect_not_observed is not proof that nothing was done (the replica can lag, and a "
        "change the relation's since column does not show appears only after a full "
        "traversal).")


class VerificationError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def matches(cell: Any, expected: str | int) -> bool:
    """Text compares trimmed and exact; an integer compares numerically (5 == "5" == 5.0)."""
    if cell is None or isinstance(cell, bool):
        return False
    if isinstance(expected, str):
        if isinstance(cell, str):
            return cell.strip() == expected
        return isinstance(cell, int) and str(cell) == expected
    number = _number(cell)
    return number is not None and number == Decimal(expected)


def field_status(row: Mapping[str, Any], path: str, expected: str | int) -> dict[str, Any]:
    """The read-back status of one field, classified once (``capability_guards.read_field``
    then ``value_state``, or ``number_state`` when ``expected`` is an integer): a field
    that cannot be read, or holds what its kind cannot read (a float, boolean or list
    where text is expected), is ``not_inspectable``, never ``empty`` or ``differs``."""
    read, value = read_field(row, path)
    state = number_state(read, value) if isinstance(expected, int) and \
        not isinstance(expected, bool) else value_state(read, value)
    if state["state"] == "unreadable":
        return {"status": "not_inspectable", "reason": state["reason"]}
    if state["state"] == "absent":
        return {"status": "field_absent" if state["reason"] == "field_absent" else "empty"}
    if matches(value, expected):
        return {"status": "equals_expected"}
    return {"status": "differs", "value_withheld": True}


def _references(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, Mapping)) or not isinstance(values, Sequence):
        raise VerificationError("invalid_references")
    if len(values) > _MAX_REFERENCES:
        raise VerificationError("too_many_references")
    out = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_REFERENCE_CHARS:
            raise VerificationError("invalid_references")
        out.append(value.strip())
    return list(dict.fromkeys(out))


def _staleness(value: Any) -> float | None:
    """None or 0..315360000 seconds; refused before any read, so nothing is recorded."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 10 * 365 * 86400):
        raise VerificationError("invalid_max_staleness", "max_staleness must be 0..315360000 "
                                "seconds")
    return value


def _copied(facts: dict[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """``facts`` with how the relation was served: a row filled from a persisted capture
    while the live source was unavailable is a past capture, never a read-back of the
    effect (decision 2026-10-04-scope-freshness rule 4), however recent the copy."""
    copied = result["source_freshness"]
    facts["source_freshness"] = {name: copied.get(name) for name in (
        "served_from", "captures_as_of", "http_status", "error_code", "label")}
    return facts


def _read_back(runtime: Any, actor: Actor, target: str,
               max_staleness: float | None) -> tuple[dict[str, Any] | None, str | None,
                                                     dict[str, Any]]:
    """(current row view or None, unknown-reason or None, mirror facts)."""
    mirror = runtime.mirror
    limit = mirror.default_max_staleness_seconds if max_staleness is None else max_staleness
    result = mirror.resolve_citation(target, actor, max_staleness=limit)
    # The case record holds no floats (Decimal-safe JSON): keep whole seconds as int.
    facts: dict[str, Any] = {"max_staleness_seconds": (int(limit) if float(limit).is_integer()
                                                       else str(limit))}
    if not result.get("ok"):
        return None, "target_citation_unresolvable", facts
    facts["relation_last_refresh_at"] = result.get("relation_last_refresh_at")
    if result.get("source_freshness"):
        return None, "relation_served_from_persisted_capture", _copied(facts, result)
    if (result.get("refresh") or {}).get("error"):
        return None, "relation_refresh_failed", facts
    if result.get("upstream_presence") in ("absent_from_latest_full_traversal",
                                           "absent_from_latest_key_scan"):
        return None, "row_not_returned_by_latest_full_traversal", facts
    current = result.get("current_citation")
    if not current:
        return None, "row_not_current_in_mirror", facts
    if result.get("status") != "current":
        result = mirror.resolve_citation(current, actor)
        if result.get("ok") and result.get("source_freshness"):
            return None, "relation_served_from_persisted_capture", _copied(facts, result)
        if not result.get("ok") or result.get("status") != "current":
            return None, "row_not_current_in_mirror", facts
    if "row" not in result:
        return None, "key_not_unique", facts
    return {**result, "citation": current}, None, facts


CLAIMS_SCHEMA = "plx-case-claim-check/v1"


# What ``CaseRuntime.claim_row`` adds to the digested view of a superseded mirror row.
_MOVED = ("status", "changed_since", "superseded_at", "current_citation")


def _case_rows(runtime: Any, actor: Actor) -> Any:
    """Re-read a cited row for ``actor`` through ``CaseRuntime.claim_row``; None unless it is
    the content the case recorded. A superseded mirror revision keeps its digested view (the
    case's recorded digest does not move) and its ``changed_since`` flag, so the gate fails
    its claims ``claim_changed_since``; the flag alone is never matched against the pin."""
    pins: dict[str, str] = {}

    def rows(handle: str) -> dict[str, Any] | None:
        try:
            resolved = runtime.claim_row(handle, actor)
        except Exception:  # noqa: BLE001 - unreadable for this actor: never leaked
            return None
        if not isinstance(resolved, Mapping):
            return None
        pin = pins.get(handle)
        cited = {key: value for key, value in resolved.items() if key not in _MOVED}
        if _citation_digest(resolved) != pin and not (
                resolved.get("changed_since") not in (None, False)
                and _citation_digest(cited) == pin):
            return None
        return dict(resolved)
    return rows, pins


def _claim_report(runtime: Any, actor: Actor, view: Mapping[str, Any],
                  only: Sequence[str] | None = None,
                  returned: Mapping[str, Any] | None = None) -> dict[str, Any]:
    buckets = view.get("findings") or {}
    findings = [item for bucket in ("supported", "contradicted", "unknown", "withheld",
                                    "citation_content_changed")
                for item in buckets.get(bucket, []) if only is None or item["id"] in only]
    # A finding whose cited content changed cannot be checked: treat it as unreadable.
    findings = [item if item.get("content") != "citation_content_changed"
                else {**item, "statement": None} for item in findings]
    gaps = [] if only is not None else list(view.get("gaps") or [])
    items, unusable = case_items(findings, gaps)
    rows, pins = _case_rows(runtime, actor)
    for item in items:
        for cite in item.get("citations") or []:
            if cite.get("handle") and cite.get("digest"):
                pins.setdefault(cite["handle"], cite["digest"])
    calculations = [calc for calc in view.get("calculations") or []
                    if calc.get("inputs_status") == "resolved"]
    for calc in calculations:
        for spec in (calc.get("inputs") or {}).values():
            cite = spec.get("citation") if isinstance(spec, Mapping) else None
            if isinstance(cite, Mapping) and cite.get("handle") and cite.get("digest"):
                pins.setdefault(cite["handle"], cite["digest"])
    return check_items(items, rows=rows, calculations=calculations,
                       contracts=getattr(runtime, "unit_contracts", None), unusable=unusable,
                       outcome=view.get("outcome") if only is None else None,
                       returned=returned)


def check_claims(runtime: Any, actor: Actor, case_id: str, *,
                 returned: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Check every quantitative claim of the case's findings readable by ``actor``.

    Read-only. ``passed`` is true only when every claim holds over the rows its
    finding cites and no finding is withheld from or changed for this actor.
    ``returned``: what tools returned to this actor that count and sum claims bind to
    (``claim_checks.returned_evidence``); without it every count is a lower bound.
    """
    if not isinstance(actor, Actor):
        raise VerificationError("invalid_actor")
    view = runtime.cases.get(actor, case_id)
    report = _claim_report(runtime, actor, view, returned=returned)
    return {"schema": CLAIMS_SCHEMA, "case_id": case_id, **report,
            "writes": 0, "note": "Claims are checked against the cited rows as re-read now; "
            "a passed claim is consistent with its evidence, not proof the source is right."}


def verify_proposal(runtime: Any, actor: Actor, case_id: str, proposal_id: str, *,
                    max_staleness: float | None = None,
                    references: Sequence[str] | None = None,
                    returned: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read back one proposal's expected effect and record the verification on the case.

    ``references`` are caller-supplied identifiers (an approval, an MC task, a
    Portal audit id) recorded verbatim as unverified; the Workbench does not
    check them and they grant nothing. ``returned``: the tool evidence the evidence
    claims bind to, as ``check_claims``.
    """
    if not isinstance(actor, Actor):
        raise VerificationError("invalid_actor")
    refs = _references(references)
    max_staleness = _staleness(max_staleness)
    view = runtime.cases.get(actor, case_id)
    if view["status"] == "closed":
        raise VerificationError("case_closed")
    proposal = next((item for item in view["proposals"] if item["id"] == proposal_id), None)
    if proposal is None:
        raise VerificationError("proposal_not_found")
    if proposal.get("content"):
        # Withheld for this reader: it cannot verify what it cannot read.
        raise VerificationError("proposal_withheld")
    effect = proposal.get("expected_effect")
    if not effect:
        raise VerificationError("proposal_has_no_expected_effect",
                                "only a proposal recorded with an expected_effect can be "
                                "verified")
    target = effect["target_citation"]
    expected = effect_values(effect)
    held_when_proposed = None
    try:
        cited_then = runtime.resolve_citation(target, actor)
    except Exception:  # noqa: BLE001 - reported as unknown below, never leaked
        cited_then = None
    if cited_then is not None and "row" in cited_then:
        held_when_proposed = effect_outcome(effect, {
            path: field_status(cited_then["row"], path, value)["status"]
            for path, value in expected.items()})[0] == "effect_observed"
    row, reason, facts = _read_back(runtime, actor, target, max_staleness)
    fields: dict[str, Any] = {}
    observed_citation = None
    if row is None:
        outcome = "unknown"
    else:
        observed_citation = row["citation"]
        fields = {path: field_status(row["row"], path, value)
                  for path, value in expected.items()}
        outcome, why = effect_outcome(effect, {p: f["status"] for p, f in fields.items()})
        if why is not None:
            reason = why
    detail = {"schema": SCHEMA, "relation": effect["relation"], "key": effect["key"],
              "fields": fields, "target_citation": target,
              "observed_citation": observed_citation,
              "held_when_proposed": held_when_proposed, **facts,
              "references": refs, "references_verified": False,
              "attribution": "not_established", "dispatched_by_workbench": False}
    cites = [observed_citation] if observed_citation else [target]
    try:
        receipt = runtime.cases.verification(actor, case_id, proposal_id, outcome, reason,
                                             detail, cites)
    except ValidationError as exc:
        raise VerificationError(str(exc)) from None
    evidence_id = proposal.get("evidence_finding_id")
    evidence_claims = (_claim_report(runtime, actor, view, [evidence_id], returned)
                       if evidence_id else None)
    return {"verification": receipt, "outcome": outcome, "reason": reason,
            "evidence_claims": evidence_claims,
            "fields": fields, "observed_citation": observed_citation,
            "held_when_proposed": held_when_proposed, "references_verified": False,
            "attribution": "not_established", "dispatched": False, "approved": False,
            "business_system_writes": 0, "note": NOTE}


__all__ = ["CLAIMS_SCHEMA", "NOTE", "SCHEMA", "VerificationError", "check_claims",
           "field_status", "matches", "verify_proposal"]
