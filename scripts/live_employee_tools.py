"""Live check of the employee-facing generic tools on a host-built projection.

    python3 -m scripts.live_employee_tools {schema,find,investigate,inline} --state-dir DIR \
        --cycle SETS [--cycle SETS ...] [--budget-seconds N] [--sample N] [--bc-books FILE] \
        [--previous-report DAILY.json]

The live proof of two employee-sourced requirements (usability,
investigate claims) as the employee met them: ``operating.host`` cycles on a
BC-only host configuration (no Portal configured), then the tools the host's ``mcp``
serves over that state (``Host.served_tools``), called as an authorized MANAGER.

Each ``--cycle`` is one host cycle (``Host.cycle``: a GET-only BC capture of the comma
list of entity sets into ``DIR/bc-capture``, then the projection build), all on the same
state directory, so a later cycle projects every set captured before it; the BC capture of
each cycle gets ``CAPTURE_SHARE`` of ``--budget-seconds`` split between the cycles (a
capture cut by its budget is still projected). The host configuration and its audiences
are written beside ``DIR``: the capture principal and the reader are granted the BC
environment, nothing else; the Portal is not configured.

The reader is ``READER`` with the clearance the Portal's MANAGER role bootstraps under the
example employee policy (``manager_clearance``), reported with ``clearance_source`` naming that
example file: an example mapping, never a production clearance. ``OUTSIDER`` holds no grant.

* ``schema`` (usability requirement (a)): ``plx.schema`` paged by its cursor to the end;
  every page that says ``truncated`` must carry a ``next_cursor``
  (``truncated_without_cursor``), and the pages together list every declared object, link,
  event and action type once (``sections.*.missing`` / ``duplicates``).
* ``find`` ((b) and (c)): for every served object type, a ``plx.find`` filter on a
  property the type does not declare (the most common property name of other types) must
  be ``unknown_property`` whose hint names the type's declared properties
  (``unknown_property_missed``; ``hint_names_valid``: its listed names, never a prose word,
  are every declared name, or declared ones plus a ``(+N more)`` count of the rest); over each type the projection
  holds, a filter on a declared property (``present``) and one that matches nothing
  (``present`` and ``absent``) must never be refused (``declared_refused``) and, as the
  Portal is not configured, must carry no Portal freshness gap
  (``portal_freshness_gaps``) but its ``source_not_configured`` gap
  (``portal_not_configured_gaps``: the reader holds the Portal relations, so a find that
  matches nothing names the Portal among the sources it searched).
* ``investigate`` (LOG-09): ``plx.investigate`` on up to
  ``--sample`` roots per family. The families are the ``ontology/plans`` plans (as the host
  loads them) with a root type the capture holds: one the projection holds or a BC entity
  set of a cycle binds (``plan_families``), so a new plan is run with no edit here. Roots
  are projected objects of the plan's root types, those with the most distinct incoming
  link types first, ties by SHA-256 of the object id. Every answer's claim checks pass
  (``claim_uncited`` 0, ``roots_failed`` 0), every family reaches a root and some family
  checks a cited value (per-family claim floors are the spec's); the outsider gets no value
  and no claim (``outsider_leaks``). Each family records its count evidence (``stages``:
  records reached, summed over its roots; ``stages_reached``: roots reaching each), the
  reasons of its gaps (``gap_codes``; ``stage_gaps``: ``<plan step>:<reason>`` of the gaps
  on a link a step walks) and of its answerability (``reason_codes``), typed codes only:
  the landed-cost walk's deposit, freight, receipt and G/L stages on sampled purchase
  orders. The families run in a rotation, oldest evidence first: ``families_age`` counts
  the runs since each family last ran to the end of its roots (0: this run), carried from
  the previous daily report (``--previous-report``); a family it does not age (never
  recorded, or no readable report) runs first, then the oldest, ties by plan id, so a
  budget stop cuts a different family each run rather than always the last in sorted
  order, and a family it cut keeps its roots reached so far. Each family's open code maps
  (``reason_codes``, ``gap_codes``, ``failure_codes``, ``stage_gaps``) fold their rarest
  codes into ``other`` (``codes_folded``) until the family serialises within
  ``FAMILY_CHARS``, so the recorded families stay under the verifier's detail cap. A
  family that reaches no root because BC holds no record of it (every entity set binding
  its root types was read to completion, in every pinned company, and holds none:
  ``root_source_empty``) does not fail the run: when it is the only shortfall the check
  names ``sources_blocked`` ``no_root_records``; a set the capture holds records of but the
  projection does not is still a failure. A pinned company the capture left unread (a
  stop before its turn, or a listing that did not show it) is named by its book in
  ``bc_companies_unread``, and then no family is ``root_source_empty``.
* ``inline``: ``plx.investigate`` with each worked inline plan (``plan``, no ``family``) of
  ``ontology/plans/_inline_examples.json`` on up to ``--sample`` roots of its root types,
  sampled as ``investigate`` samples them. Each plan records the ``investigate`` family
  stats under ``plans`` plus ``outcomes``: per root, ``answered_cited`` (an answer with a
  checked claim), ``answered_uncited`` (an answer with none: a filter nothing matched, a
  stage withheld) or ``refused:<error code>``. An outcome other than the example's
  ``expect`` (``answered``, or the typed refusal ``plan_<problem>`` it names) counts in
  ``plans_unexpected``; the claim gate and the outsider probe are ``investigate``'s, and
  every plan reaches a root and some plan checks a claim, or ``root_source_empty`` blocks.

Every answer is validated against its frozen contract (``contract_failures``). A BC cycle
that did not answer is a ``blocked_upstream`` part (the other checks still run); with no
projected object the ``investigate`` check names ``sources_blocked``. A BC throttle (429/503:
a cycle that failed with one, or a capture it stopped part-way) is one ``blocked_upstream``
part per entity set it left unread, typed ``upstream_throttled`` with its HTTP status, and
summed in ``upstream_throttled`` (``entity_sets``, ``http_statuses``): such a cycle never
passes. When the only shortfall of ``investigate`` is reach (a family with no root or no
claim) and every BC read left incomplete traces to a throttle (no other outage, budget stop
or failed unit), it names ``sources_blocked`` ``upstream_throttled``; a wrong, uncited or
leaked answer still fails. Exit 0 when the check holds or is blocked upstream, 1 otherwise
(a raise prints only its class). The output is counts, booleans, type-free codes and
states: never a value, number, name, identifier or citation. Evidence: injected-transport
tests (``tests/test_live_employee_tools.py``); live-unverified until the verifier records
a result.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Mapping, Sequence

PROBE = "live-employee-tools/v1"
ROOT = Path(__file__).resolve().parent.parent
TENANT = "plx"
CAPTURE_PRINCIPAL = "svc:workbench-host"
READER = "staff:manager"
OUTSIDER = "staff:no-grant"
ROLE = "MANAGER"
EXAMPLE_POLICY = ROOT / "examples" / "employee_permission_policy.example.json"
PORTAL_SOURCE = "portal_api"  # the schema's source name of the Portal bindings
CHECKS = ("schema", "find", "investigate", "inline")
CAPTURE_SHARE = 0.6   # of --budget-seconds, split between the cycles' BC captures
PAGE_CAP = 200        # plx.schema pages followed at most
UNDECLARED_FALLBACK = "undeclared_property"
CLEARANCE_SOURCE = EXAMPLE_POLICY.relative_to(ROOT).as_posix()
THROTTLE_STATUSES = (429, 503)
THROTTLED = "upstream_throttled"
NO_ROOT_RECORDS = "no_root_records"
INVESTIGATE_CHECK = "tools.investigate_bc_claims"  # its live-check id: the previous run
FAMILY_CHARS = 1600   # a family's serialised stats at most (FAMILY_CODE_MAPS folded)
FAMILY_CODE_MAPS = ("reason_codes", "gap_codes", "failure_codes", "stage_gaps")
_ENVIRONMENT = re.compile(r"[A-Za-z0-9_-]{1,50}")
_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]{0,79}")
_SET = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")
_MORE = re.compile(r"\(\+(\d+) more\)")


class BudgetExhausted(Exception):
    pass


def manager_clearance() -> str:
    """The clearance the Portal MANAGER role bootstraps (``LEGACY_ROLE_BOOTSTRAP``) under
    the example employee policy's ``clearance_by_access_level``."""
    from operating.employee_permissions_portal import LEGACY_ROLE_BOOTSTRAP
    policy = json.loads(EXAMPLE_POLICY.read_text("utf-8"))
    return policy["clearance_by_access_level"][LEGACY_ROLE_BOOTSTRAP[ROLE]]


def portal_families(schema: Any) -> list[str]:
    """``<schema>.*`` of every relation the schema binds to the Portal."""
    return sorted({b["relation"].split(".", 1)[0] + ".*"
                   for spec in schema.object_types.values()
                   for b in spec.get("bindings", ())
                   if b.get("source") == PORTAL_SOURCE and "." in b.get("relation", "")})


def write_configs(work: Path, cycles: Sequence[Sequence[str]], schema: Any, *,
                  environment: str, budget_seconds: float | None, books: str | None
                  ) -> list[Path]:
    """One BC-only ``plx-host-config/v1`` per cycle in ``work`` (no Portal, mail or
    SharePoint), with audiences granting the BC environment to the capture principal and
    the reader, and the Portal relations to the reader (a MANAGER holds the Portal too: a
    find then names the Portal among the sources it searched, which the host does not
    read)."""
    from operating import host as hm
    work.mkdir(parents=True, exist_ok=True)
    env = environment.lower() if _ENVIRONMENT.fullmatch(environment) else "production"
    documents = {
        "audience.json": {"schema": "plx-live-source-audience/v1", "tenant": TENANT,
                          "default_classification": "confidential",
                          "audiences": {f"bc:{env}:*": [CAPTURE_PRINCIPAL, READER]}},
        "mirror.json": {"schema": "plx-source-mirror-audience/v1", "tenant": TENANT,
                        "audiences": {family: [READER]
                                      for family in portal_families(schema)}}}
    for name, document in documents.items():
        (work / name).write_text(json.dumps(document), "utf-8")
    paths = []
    for index, entity_sets in enumerate(cycles):
        bc: dict[str, Any] = {"entity_sets": list(entity_sets)}
        if budget_seconds is not None:
            bc["budget_seconds"] = budget_seconds
        if books is not None:
            bc["books"] = str(Path(books).resolve())
        path = work / f"host-{index + 1}.json"
        path.write_text(json.dumps({
            "schema": hm.CONFIG_SCHEMA, "tenant": TENANT,
            "capture_principal": CAPTURE_PRINCIPAL, "capture_audience": "audience.json",
            "live_audience": "audience.json", "mirror_audience": "mirror.json", "bc": bc}),
            "utf-8")
        paths.append(path)
    return paths


def run_cycles(state: Path, configs: Sequence[Path], cycles: Sequence[Sequence[str]], *,
               environ: Mapping[str, str] | None, sender: Any,
               sleep: Callable[[float], None]) -> tuple[Any, dict[str, Any]]:
    """Each config's host cycle on ``state``; the last host and the cycles' counts.

    ``untraced`` counts the cycles left incomplete by anything but a throttle (another
    outage, a budget stop, a unit that failed or paused, a pinned company not listed
    unless a throttle stopped the listing).
    The company listing counts only through the pinned companies it did not show
    (``pinned_companies_missing``): an unpinned company is never captured by design, and
    an incomplete listing (``companies_listing_complete`` false) that still shows every
    pinned company left nothing the checks read unread."""
    from operating import host as hm
    summary: dict[str, Any] = {"cycles": 0, "states": {}, "units": 0, "units_complete": 0,
                               "requests": 0, "reasons": [], "capture_errors": 0,
                               "projection_failed": 0, "portal_states": [],
                               "blocked_upstream": [], "untraced": 0,
                               "throttled_sets": set(), "throttled_statuses": set()}
    host = None
    for index, (path, entity_sets) in enumerate(zip(configs, cycles)):
        host = hm.Host(state, hm.HostConfig.from_file(path), environ=environ, sender=sender,
                       sleep=sleep)
        report = host.cycle()
        summary["cycles"] += 1
        bc, portal = report["sources"]["bc"], report["sources"]["portal"]
        states = summary["states"]
        states[bc["state"]] = states.get(bc["state"], 0) + 1
        counts = bc.get("counts") or {}
        for key in ("units", "units_complete", "requests"):
            summary[key] += int(counts.get(key) or 0)
        summary["reasons"] += [r for r in bc.get("reasons", ()) if _CODE.fullmatch(r)]
        if portal["state"] not in summary["portal_states"]:
            summary["portal_states"].append(portal["state"])
        summary["projection_failed"] += report["projection"].get("state") == "failed"
        name = f"bc_cycle_{index + 1}"
        if bc["state"] == "failed":
            outage = bc.get("outage")
            if outage is None:  # not an upstream answer: a product failure, never blocked
                summary["capture_errors"] += 1
                continue
            status = outage.get("http_status")
            if status in THROTTLE_STATUSES:  # nothing of the cycle was read
                _throttled(summary, name, status, entity_sets)
                continue
            part: dict[str, Any] = {"part": name, "source": "business-central"}
            if isinstance(status, int):
                part["http_status"] = status
            if isinstance(outage.get("error_code"), str) \
                    and _CODE.fullmatch(outage["error_code"]):
                part["error_code"] = outage["error_code"]
            summary["blocked_upstream"].append(part)
            summary["untraced"] += 1
            continue
        throttle = bc.get("throttle") or {}
        if throttle.get("http_status") in THROTTLE_STATUSES:
            _throttled(summary, name, throttle["http_status"], throttle.get("entity_sets", ()))
        # Units reached but not complete (no reached count: every planned unit); the
        # units a stop left unreached are the throttle's (its entity_sets) or the stop
        # reason's.
        incomplete = (int(counts.get("units_reached", counts.get("units")) or 0)
                      - int(counts.get("units_complete") or 0)
                      - int(counts.get("units_not_exposed") or 0)
                      - int(throttle.get("units") or 0))
        # a throttled company listing explains the pinned companies it did not show
        unlisted = counts.get("pinned_companies_missing") and \
            not throttle.get("companies_listing")
        summary["untraced"] += bool(
            incomplete > 0 or unlisted or set(bc.get("reasons", ())) - {"throttled"})
    summary["reasons"] = sorted(set(summary["reasons"]))
    return host, summary


def _throttled(summary: dict[str, Any], name: str, status: int, entity_sets: Sequence[str]
               ) -> None:
    """One ``upstream_throttled`` part per entity set a BC throttle left unread."""
    sets = sorted({s if isinstance(s, str) and _SET.fullmatch(s) else "other"
                   for s in entity_sets}) or [None]
    for entity_set in sets:
        summary["blocked_upstream"].append({
            "part": name if entity_set is None else f"{name}:{entity_set}",
            "source": "business-central", "http_status": status, "error_code": THROTTLED})
    summary["throttled_sets"].update(s for s in sets if s is not None)
    summary["throttled_statuses"].add(status)


class Caller:
    """The served host's ``call`` for one reader: contract validation, error codes and
    the budget, counted."""

    def __init__(self, served: Any, actor: Any, report: dict[str, Any],
                 deadline: float | None, monotonic: Callable[[], float]) -> None:
        from operating.ontology_tools import load_contracts
        self.served, self.actor, self.report = served, actor, report
        self.deadline, self.monotonic = deadline, monotonic
        self.contracts = load_contracts()

    def __call__(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        from ontology.json_contract import validate
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise BudgetExhausted()
        out = self.served.call(name, dict(arguments), self.actor)
        self.report["calls"] += 1
        if validate(out, self.contracts["tools"][name]["output"], root=self.contracts,
                    limit=1):
            self.report["contract_failures"] += 1
        if "error" in out:
            codes = self.report["error_codes"]
            codes[out["error"]["code"]] = codes.get(out["error"]["code"], 0) + 1
        return out


# ------------------------------------------------------------------ the checks

_SECTIONS = ("object_types", "link_types", "event_types", "action_types")


def schema_check(call: Callable[..., dict], schema: Any) -> dict[str, Any]:
    """``plx.schema`` paged to the end: what each section lists against what is declared."""
    listed: dict[str, list[str]] = {key: [] for key in _SECTIONS}
    pages = truncated_without_cursor = 0
    cursor, complete = None, False
    while pages < PAGE_CAP:
        out = call("plx.schema", {} if cursor is None else {"cursor": cursor})
        pages += 1
        if "error" in out:
            cursor = None
            break
        for key in _SECTIONS:
            listed[key] += [entry["id"] for entry in out.get(key, ())]
        completeness = out.get("completeness") or {}
        cursor = completeness.get("next_cursor")
        if completeness.get("truncated") and not cursor:
            truncated_without_cursor += 1
        if not cursor:
            complete = completeness.get("complete") is True
            break
    sections = {}
    for key in _SECTIONS:
        declared, seen = set(getattr(schema, key)), set(listed[key])
        sections[key] = {"declared": len(declared), "listed": len(seen),
                         "missing": len(declared - seen), "undeclared": len(seen - declared),
                         "duplicates": len(listed[key]) - len(seen)}
    return {"pages": pages, "truncated_without_cursor": truncated_without_cursor,
            "page_cap_hit": bool(cursor), "last_page_complete": complete,
            "sections": sections,
            "missing_total": sum(s["missing"] for s in sections.values()),
            "duplicates_total": sum(s["duplicates"] for s in sections.values())}


def _hint_lists(hint: Any, own: set[str]) -> bool:
    """The hint lists the type's declared properties: all of them, or the ones it lists
    plus a ``(+N more)`` that counts exactly the rest (never one name in passing). Only
    the comma list after the hint's colon counts, never a word of its prose ('filter',
    'plx.schema'), and a listed name the type does not declare fails it."""
    hint = hint if isinstance(hint, str) else ""
    if not own:
        return "declares no properties" in hint
    if ": " not in hint:
        return False
    body = hint.split(": ", 1)[1].split(";", 1)[0]
    more = _MORE.search(body)
    names = [name.strip() for name in body[:more.start() if more else None].split(",")]
    listed = set(names)
    if len(listed) != len(names) or not listed <= own:
        return False
    return listed == own or (more is not None and len(listed) + int(more[1]) == len(own))


def _undeclared(own: set[str], common: Sequence[str]) -> str:
    return next((name for name in common if name not in own), UNDECLARED_FALLBACK)


def find_check(call: Callable[..., dict], schema: Any, held_types: Sequence[str]
               ) -> dict[str, Any]:
    """``unknown_property`` on every served type, then declared-property filters over the
    types the projection holds: never refused, no Portal freshness gap."""
    names = Counter(p["name"] for spec in schema.object_types.values()
                    for p in spec.get("properties", ()))
    common = [name for name, _ in sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))]
    report: dict[str, Any] = {"types_probed": 0, "types_not_served": 0, "unknown_property": 0,
                              "unknown_property_missed": 0, "missed_outcomes": {},
                              "hint_names_valid": 0, "declared_probed": 0,
                              "declared_refused": 0, "declared_outcomes": {},
                              "finds_checked": 0, "portal_freshness_gaps": 0,
                              "portal_not_configured_gaps": 0, "freshness_gaps": {}}
    for type_id, spec in sorted(schema.object_types.items()):
        if spec.get("availability", {}).get("state") == "not_served":
            report["types_not_served"] += 1
            continue
        own = {p["name"] for p in spec.get("properties", ())}
        out = call("plx.find", {"object_type": type_id, "filters": [
            {"property": _undeclared(own, common), "op": "eq", "value": "x"}]})
        report["types_probed"] += 1
        error = out.get("error") or {}
        if error.get("code") == "unknown_property":
            report["unknown_property"] += 1
            report["hint_names_valid"] += _hint_lists(error.get("hint"), own)
            continue
        report["unknown_property_missed"] += 1
        outcome = error.get("code") or out.get("status") or "none"
        report["missed_outcomes"][outcome] = report["missed_outcomes"].get(outcome, 0) + 1
    for type_id in held_types:
        spec = schema.object_types.get(type_id) or {}
        own = sorted(p["name"] for p in spec.get("properties", ()))
        if not own:
            continue
        # A declared property present (a listing), then present and absent at once (a
        # find that matches nothing: its gaps name every source searched).
        for filters in ([{"property": own[0], "op": "present"}],
                        [{"property": own[0], "op": "present"},
                         {"property": own[0], "op": "absent"}]):
            out = call("plx.find", {"object_type": type_id, "limit": 5, "filters": filters})
            report["declared_probed"] += 1
            code = (out.get("error") or {}).get("code")
            report["declared_refused"] += code == "unknown_property"
            outcome = code or out.get("status") or "none"
            outcomes = report["declared_outcomes"]
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if code is not None:
                continue
            report["finds_checked"] += 1
            for gap in out.get("gaps", ()):
                if gap.get("subject") != PORTAL_SOURCE:
                    continue
                reason = gap.get("reason")
                report["freshness_gaps"][reason] = report["freshness_gaps"].get(reason, 0) + 1
                if reason == "source_not_configured":
                    report["portal_not_configured_gaps"] += 1
                else:
                    report["portal_freshness_gaps"] += 1
    return report


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bc_bound_types(schema: Any, cycles: Sequence[Sequence[str]]) -> set[str]:
    """The object types a BC binding of the schema projects from an entity set a cycle
    captures."""
    sets = {s for cycle in cycles for s in cycle}
    return {type_id for type_id, spec in schema.object_types.items()
            for b in spec.get("bindings", ())
            if b.get("source") == "bc" and b.get("relation") in sets}


def plan_families(schema: Any, types: set[str]) -> dict[str, Mapping[str, Any]]:
    """{plan id: plan document} of every plan the host loads (``load_plans``; a broken
    plan raises) with a root type in ``types``."""
    from operating.ontology_tools_investigate import load_plans, plan_root_types
    return {pid: plan.doc for pid, plan in sorted(load_plans(schema=schema).items())
            if types.intersection(plan_root_types(plan.doc, schema))}


def previous_ages(path: str | Path | None) -> dict[str, int]:
    """``families_age`` the previous verifier report recorded for this check: plan ids
    and non-negative run counts only; anything else is dropped (that family then runs
    first, as one never recorded); empty without a readable report."""
    try:
        report = json.loads(Path(path).read_text("utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    checks = report.get("checks") if isinstance(report, Mapping) else None
    for check in checks if isinstance(checks, list) else ():
        if isinstance(check, Mapping) and check.get("id") == INVESTIGATE_CHECK:
            detail = check.get("detail")
            fields = detail.get("fields") if isinstance(detail, Mapping) else None
            ages = fields.get("families_age") if isinstance(fields, Mapping) else None
            return {f: n for f, n in ages.items()
                    if isinstance(f, str) and _CODE.fullmatch(f) and isinstance(n, int)
                    and not isinstance(n, bool) and n >= 0} \
                if isinstance(ages, Mapping) else {}
    return {}


def rotation(families: Mapping[str, Mapping[str, Any]], ages: Mapping[str, int]
             ) -> dict[str, Mapping[str, Any]]:
    """``families`` oldest evidence first: those ``ages`` does not hold, then the
    highest age, ties by plan id."""
    return {f: families[f] for f in sorted(families, key=lambda f: (f in ages,
                                                                     -ages.get(f, 0), f))}


def empty_sets(store_path: Path, audience_path: Path, cycles: Sequence[Sequence[str]],
               pinned: Mapping[str, str]) -> tuple[set[str], list[str]]:
    """The captured entity sets BC answered empty, and the books of the ``pinned``
    companies (``{company GUID: book}``) the capture left unread. A set is empty when
    every pinned company's unit of it has a latest run read to completion and none holds
    a current record; a set read in part, or not at all, in any pinned company is never
    empty. A pinned company is unread when a captured set of it has no run (a stop before
    its turn, or a listing that did not show it): no set is empty then."""
    from integrations.capture_store import CaptureStore, bc_unit
    from integrations.live_source_guard import LiveSourceAudience
    captured = sorted({s for cycle in cycles for s in cycle})
    if not store_path.is_file():
        return set(), sorted(set(pinned.values()))
    store = CaptureStore.open_read_only(store_path, LiveSourceAudience.from_file(
        audience_path))
    try:
        empty, unread = set(captured), set()
        for company, book in pinned.items():
            for entity_set in captured:
                unit = bc_unit(company, entity_set)
                run = store.run("bc", unit)
                if run is None:
                    unread.add(book)
                if run is None or run["state"] != "complete" \
                        or store.current_versions("bc", unit):
                    empty.discard(entity_set)
    finally:
        store.close()
    return (set() if unread or not pinned else empty), sorted(unread)


def pinned_books(books: str | None, environ: Mapping[str, str]) -> dict[str, str]:
    """``{company GUID: book}`` the books (``--bc-books``, else the pinned map) pin in the
    environment the capture reads."""
    from integrations.bc_read import load_company_books
    environment = (environ.get("BC_ENVIRONMENT") or "production").lower()
    return load_company_books(books).get(environment, {})


def root_sets(schema: Any, plan: Mapping[str, Any], cycles: Sequence[Sequence[str]]
              ) -> set[str]:
    """The captured BC entity sets that bind the plan's root types."""
    from operating.ontology_tools_investigate import plan_root_types
    sets = {s for cycle in cycles for s in cycle}
    return {b["relation"] for t in plan_root_types(plan, schema)
            for b in (schema.object_types.get(t) or {}).get("bindings", ())
            if b.get("source") == "bc" and b.get("relation") in sets}


def _fit(stats: dict[str, Any]) -> None:
    """Fold the rarest codes of the fullest code map into ``other`` until the family
    serialises within ``FAMILY_CHARS`` (``codes_folded`` counts them)."""
    while len(json.dumps(stats, sort_keys=True)) > FAMILY_CHARS:
        maps = [stats[name] for name in FAMILY_CODE_MAPS
                if any(code != "other" for code in stats[name])]
        if not maps:
            return
        codes = max(maps, key=len)
        code = min((c for c in codes if c != "other"), key=lambda c: (codes[c], -len(c), c))
        codes["other"] = codes.get("other", 0) + codes.pop(code)
        stats["codes_folded"] = stats.get("codes_folded", 0) + 1


def _step_links(plan: Mapping[str, Any], schema: Any) -> dict[str, str]:
    """{link type: id of the first plan step walking it}: a gap whose subject names the
    link is that step's (``stage_gaps``); a hop naming a link by its navigation name walks
    the link it names on each root type."""
    from operating.ontology_tools_investigate import plan_root_types
    from operating.ontology_tools_traverse import navigation_hops
    links: dict[str, str] = {}
    types = sorted(plan_root_types(plan, schema)) if schema is not None else []
    for step in plan.get("steps", ()):
        for hop in (step.get("args") or {}).get("path", ())[:1]:
            for link_type in ([hop["link_type"]] if "link_type" in hop else
                              [lt for t in types for lt, _ in
                               navigation_hops(schema, t, hop["link"])]):
                links.setdefault(link_type, step["id"])
    return links


def sample_roots(conn: sqlite3.Connection | None, types: Sequence[str], sample: int
                 ) -> list[str]:
    """Projected objects of ``types``: most distinct incoming link types first (a PO with
    its receipt and invoice before a bare one), ties by SHA-256 of the object id; none
    without a projection."""
    if conn is None:
        return []
    marks = ",".join("?" * len(types))
    rows = conn.execute(
        "SELECT o.object_id, COUNT(DISTINCT l.link_type) FROM objects o LEFT JOIN links l "
        f"ON l.to_object = o.object_id WHERE o.object_type IN ({marks}) GROUP BY o.object_id",
        list(types)).fetchall()
    return [oid for oid, _ in sorted(rows, key=lambda r: (-r[1], _hash(r[0])))][:sample]


def _add(counts: dict[str, int], key: Any) -> None:
    key = key if isinstance(key, str) and _CODE.fullmatch(key) else "other"
    counts[key] = counts.get(key, 0) + 1


def investigate_check(report: dict[str, Any], reader: Callable[..., dict],
                      outsider: Callable[..., dict], conn: sqlite3.Connection | None,
                      families: Mapping[str, Mapping[str, Any]], sample: int, *,
                      ages: Mapping[str, int], empty: Callable[[Mapping[str, Any]], bool],
                      schema: Any = None) -> None:
    """``plx.investigate`` per family ({plan id: plan document}, in the order given) on
    sampled roots, as the reader and the outsider, into ``report`` as it goes (a budget
    stop keeps what ran). ``ages``: the previous run's ``families_age``; ``empty``: the
    plan's root types are bound only by entity sets BC answered empty; ``schema`` resolves
    a plan's root ``links`` and hops named by navigation name."""
    from operating.ontology_tools_investigate import plan_root_types
    report.update({"families": {}, "families_run": list(families), "claim_uncited": 0,
                   "roots_failed": 0, "roots_investigated": 0, "outsider_probed": 0,
                   "outsider_leaks": 0, "outsider_states": {},
                   "families_age": {f: ages[f] + 1 for f in families if f in ages}})
    for family, plan in families.items():
        links = _step_links(plan, schema)
        stats: dict[str, Any] = {"roots": 0, "errors": 0, "claims": 0, "cited_values": 0,
                                 "roots_passed": 0, "roots_failed": 0, "roots_unchecked": 0,
                                 "failure_codes": {}, "value_checks": {}, "answerability": {},
                                 "stages": {}, "stages_reached": {}, "gap_codes": {},
                                 "stage_gaps": {}, "reason_codes": {}}
        report["families"][family] = stats
        try:
            roots = sample_roots(conn, sorted(plan_root_types(plan, schema)), sample)
            if not roots and empty(plan):
                stats["root_source_empty"] = True
            for root in roots:
                _investigate_root(report, stats, links, reader, outsider,
                                  {"family": family}, root)
            report["families_age"][family] = 0
        finally:
            _fit(stats)
            report["roots_failed"] += stats["roots_failed"]


def inline_check(report: dict[str, Any], reader: Callable[..., dict],
                 outsider: Callable[..., dict], conn: sqlite3.Connection | None,
                 examples: Sequence[Mapping[str, Any]], sample: int, *,
                 empty: Callable[[Mapping[str, Any]], bool], schema: Any = None) -> None:
    """``plx.investigate`` with each example's inline plan on sampled roots, as the reader
    and the outsider, into ``report`` as it goes; ``empty`` and ``schema`` as for
    ``investigate_check``."""
    from operating.ontology_tools_investigate import plan_root_types
    report.update({"plans": {}, "plans_run": [e["plan"]["id"] for e in examples],
                   "plans_unexpected": 0, "claim_uncited": 0, "roots_failed": 0,
                   "roots_investigated": 0, "outsider_probed": 0, "outsider_leaks": 0,
                   "outsider_states": {}})
    for example in examples:
        plan, expect = example["plan"], example["expect"]
        stats: dict[str, Any] = {"roots": 0, "errors": 0, "claims": 0, "cited_values": 0,
                                 "roots_passed": 0, "roots_failed": 0, "roots_unchecked": 0,
                                 "failure_codes": {}, "value_checks": {}, "answerability": {},
                                 "stages": {}, "stages_reached": {}, "gap_codes": {},
                                 "stage_gaps": {}, "reason_codes": {}, "outcomes": {}}
        report["plans"][plan["id"]] = stats
        try:
            roots = sample_roots(conn, sorted(plan_root_types(plan, schema)), sample)
            if not roots and empty(plan):
                stats["root_source_empty"] = True
            for root in roots:
                _investigate_root(report, stats, _step_links(plan, schema), reader, outsider,
                                  {"plan": plan}, root)
        finally:
            report["plans_unexpected"] += sum(
                n for outcome, n in stats["outcomes"].items()
                if outcome != f"refused:{expect}" and not (
                    expect == "answered" and outcome.startswith("answered_")))
            _fit(stats)
            report["roots_failed"] += stats["roots_failed"]


def inline_examples() -> list[dict[str, Any]]:
    """The worked inline plans and the outcome each expects (``INLINE_EXAMPLES``)."""
    from operating.ontology_tools_investigate import INLINE_EXAMPLES
    return json.loads(INLINE_EXAMPLES.read_text("utf-8"))["examples"]


def _investigate_root(report: dict[str, Any], stats: dict[str, Any], links: Mapping[str, str],
                      reader: Callable[..., dict], outsider: Callable[..., dict],
                      question: Mapping[str, Any], root: str) -> None:
    """One root: ``question`` is ``{"family": id}`` or ``{"plan": document}``; a stats map
    with ``outcomes`` (an inline plan's) also counts the root's outcome."""
    stats["roots"] += 1
    out = reader("plx.investigate", {**question, "root": root})
    outcomes = stats.get("outcomes")
    if "error" in out:
        stats["errors"] += 1
        if outcomes is not None:
            _add(outcomes, f"refused:{out['error'].get('code')}")
        return
    report["roots_investigated"] += 1
    answerability = out.get("answerability") or {}
    _add(stats["answerability"], answerability.get("state"))
    for reason in answerability.get("reasons", ()):
        _add(stats["reason_codes"], reason)
    for gap in out.get("gaps", ()):
        _add(stats["gap_codes"], gap.get("reason"))
        subject = gap.get("subject")
        step = links.get(subject.split(" ", 1)[0]) if isinstance(subject, str) else None
        reason = gap.get("reason")
        if step is not None and isinstance(reason, str) and _CODE.fullmatch(reason):
            key = f"{step}:{reason}"
            _add(stats["stage_gaps"], key if len(key) <= 64 else "other")
    checks = out.get("claim_checks") or {}
    stats["claims"] += int(checks.get("claims") or 0)
    for failure in checks.get("failures", ()):
        _add(stats["failure_codes"], failure.get("code"))
        report["claim_uncited"] += failure.get("code") == "claim_uncited"
    if not checks.get("checked"):
        stats["roots_unchecked"] += 1
    elif checks.get("passed") is True:
        stats["roots_passed"] += 1
    else:
        stats["roots_failed"] += 1
    for entry in out.get("answer", ()):
        if entry.get("kind") == "property" and entry.get("value") is not None:
            stats["cited_values"] += 1
            _add(stats["value_checks"], entry.get("check"))
        elif entry.get("kind") == "count" and isinstance(entry.get("value"), int):
            name = entry.get("name")
            name = name if isinstance(name, str) and _CODE.fullmatch(name) else "other"
            stats["stages"][name] = stats["stages"].get(name, 0) + entry["value"]
            _add(stats["stages_reached"], name)
    if outcomes is not None:
        _add(outcomes, "answered_cited" if checks.get("claims") else "answered_uncited")
    refused = outsider("plx.investigate", {**question, "root": root})
    report["outsider_probed"] += 1
    _add(report["outsider_states"], "error" if "error" in refused else
         (refused.get("answerability") or {}).get("state"))
    report["outsider_leaks"] += (
        len(refused.get("values") or ())
        + int((refused.get("claim_checks") or {}).get("claims") or 0)
        + sum(1 for e in refused.get("answer", ()) if e.get("value") is not None))


# ------------------------------------------------------------------ the run

def _wrong(check: str, report: Mapping[str, Any]) -> bool:
    """A product failure or a wrong, uncited or leaked answer: never blocked."""
    if report["contract_failures"] or report["capture_errors"] or report["budget_exhausted"] \
            or report["projection_failed"]:
        return True
    if check == "schema":
        return not (report["truncated_without_cursor"] == 0 and report["missing_total"] == 0
                    and report["duplicates_total"] == 0 and not report["page_cap_hit"])
    if check == "find":
        return not (report["unknown_property_missed"] == 0 and report["declared_refused"] == 0
                    and report["hint_names_valid"] == report["unknown_property"]
                    and report["portal_freshness_gaps"] == 0)
    if check == "inline":
        return not (report["claim_uncited"] == 0 and report["roots_failed"] == 0
                    and report["outsider_leaks"] == 0 and report["plans_unexpected"] == 0)
    return not (report["claim_uncited"] == 0 and report["roots_failed"] == 0
                and report["outsider_leaks"] == 0
                and all(f["errors"] == 0 for f in report["families"].values()))


def _short(check: str, report: Mapping[str, Any], *, empty_ok: bool = False) -> bool:
    """Not reached: no family (``inline``: plan) ran, one reached no root or none checked a
    claim.
    A family whose roots answer no claim on these sources (decision evidence without
    Mission Control) is not short: per-family claim floors are the spec's. ``empty_ok``:
    a family with no root because BC holds none (``root_source_empty``) is not counted."""
    groups = {"investigate": "families", "inline": "plans"}.get(check)
    families = [f for f in report[groups].values()
                if not (empty_ok and f.get("root_source_empty"))] if groups else None
    return families is not None and not (
        families and all(f["roots"] >= 1 for f in families)
        and any(f["claims"] >= 1 for f in families))


def _verdict(check: str, report: dict[str, Any], capture: Mapping[str, Any]) -> bool:
    """``ok``; names ``sources_blocked`` ``no_root_records`` when the families BC holds
    no root record of explain every root ``investigate`` did not reach, else
    ``upstream_throttled`` when a throttle explains them."""
    throttle_only = (bool(capture["throttled_statuses"]) and not capture["untraced"]
                     and all(p.get("error_code") == THROTTLED
                             for p in report["blocked_upstream"]))
    if "sources_blocked" in report:  # nothing projected because BC did not answer
        if throttle_only:
            report["sources_blocked"] = THROTTLED
        return not (report["capture_errors"] or report["projection_failed"])
    if _wrong(check, report):
        return False
    if not _short(check, report):
        return True
    if not _short(check, report, empty_ok=True):
        report["sources_blocked"] = NO_ROOT_RECORDS
        return True
    if throttle_only:
        report["sources_blocked"] = THROTTLED
        return True
    return False


def run(check: str, state: Path, cycles: Sequence[Sequence[str]], *,
        budget_seconds: float | None, sample: int, books: str | None,
        previous_report: str | None = None, environ: Mapping[str, str] | None,
        sender: Any, sleep: Callable[[float], None],
        monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    from ontology.core import Actor
    from ontology.schema import load_schema
    from operating import host as hm
    from operating.ontology_tools import SchemaView

    started = monotonic()
    env = os.environ if environ is None else environ
    capture_budget = (None if budget_seconds is None
                      else budget_seconds * CAPTURE_SHARE / len(cycles))
    schema = SchemaView.from_compiled(load_schema())
    configs = write_configs(state.parent / (state.name + "-config"), cycles, schema,
                            environment=env.get("BC_ENVIRONMENT") or "production",
                            budget_seconds=capture_budget, books=books)
    host, capture = run_cycles(state, configs, cycles, environ=environ, sender=sender,
                               sleep=sleep)
    report: dict[str, Any] = {
        "probe": PROBE, "check": check, "role": ROLE, "calls": 0, "contract_failures": 0,
        "error_codes": {}, "budget_exhausted": False,
        "portal_state": capture["portal_states"][0] if len(capture["portal_states"]) == 1
        else "mixed",
        "bc_capture": {k: capture[k] for k in ("cycles", "states", "units", "units_complete",
                                               "requests", "reasons")},
        "capture_errors": capture["capture_errors"],
        "projection_failed": capture["projection_failed"],
        "blocked_upstream": capture["blocked_upstream"]}
    if capture["throttled_statuses"]:
        report[THROTTLED] = {"entity_sets": sorted(capture["throttled_sets"]),
                             "http_statuses": sorted(capture["throttled_statuses"])}
    store = hm.projection_path(state)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True) if store.is_file() else None
    try:
        held = [] if conn is None else [r[0] for r in conn.execute(
            "SELECT object_type FROM objects GROUP BY object_type ORDER BY object_type")]
        report["objects"] = 0 if conn is None else conn.execute(
            "SELECT COUNT(*) FROM objects").fetchone()[0]
        report["object_types_held"] = len(held)
        deadline = None if budget_seconds is None else started + budget_seconds
        clearance = manager_clearance()
        manager = Actor(TENANT, frozenset({READER}), clearance)
        report["reader_clearance"] = clearance
        report["clearance_source"] = CLEARANCE_SOURCE
        with host.served_tools(manager) as served:
            call = Caller(served, manager, report, deadline, monotonic)
            try:
                if check == "schema":
                    report.update(schema_check(call, schema))
                elif check == "find":
                    report.update(find_check(call, schema, held))
                elif report["objects"] == 0 and report["blocked_upstream"]:
                    report["sources_blocked"] = "no_record_source"
                else:
                    outsider = Actor(TENANT, frozenset({OUTSIDER}), clearance)
                    families = plan_families(schema, set(held) | bc_bound_types(schema, cycles))
                    answered_empty, report["bc_companies_unread"] = empty_sets(
                        state / hm.BC_DIR / hm.BC_FILE, configs[0].parent / "audience.json",
                        cycles, pinned_books(books, env))

                    def empty(plan: Mapping[str, Any]) -> bool:
                        sets = root_sets(schema, plan, cycles)
                        return bool(sets) and sets <= answered_empty

                    with host.served_tools(outsider) as refused:
                        denied = Caller(refused, outsider, report, deadline, monotonic)
                        ages = previous_ages(previous_report)
                        if check == "inline":
                            inline_check(report, call, denied, conn, inline_examples(),
                                         sample, empty=empty, schema=schema)
                        else:
                            investigate_check(report, call, denied, conn,
                                              rotation(families, ages), sample, ages=ages,
                                              empty=empty, schema=schema)
            except BudgetExhausted:
                report["budget_exhausted"] = True
    finally:
        if conn is not None:
            conn.close()
    report["seconds"] = round(monotonic() - started, 3)
    # Nothing measured, or only roots a throttle kept out of reach, because BC did not
    # answer: blocked (the verifier's blocked_when), never a pass, unless the product
    # itself failed.
    report["ok"] = _verdict(check, report, capture)
    return report


def _entity_sets(text: str) -> list[str]:
    sets = [s.strip() for s in text.split(",") if s.strip()]
    if not sets:
        raise argparse.ArgumentTypeError("a cycle names at least one entity set")
    return sets


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.live_employee_tools")
    parser.add_argument("check", choices=CHECKS)
    parser.add_argument("--state-dir", required=True, type=Path,
                        help="throwaway host state directory (outside any git checkout)")
    parser.add_argument("--cycle", action="append", required=True, type=_entity_sets,
                        help="comma list of BC entity sets one host cycle captures")
    parser.add_argument("--budget-seconds", type=float, default=None)
    parser.add_argument("--sample", type=int, default=3, help="investigate roots per family")
    parser.add_argument("--bc-books", default=None,
                        help="plx-bc-company-books/v1 file (default: the pinned books)")
    parser.add_argument("--previous-report", default=None,
                        help="the last daily verifier report (investigate: families_age)")
    args = parser.parse_args(argv)
    if args.sample < 1:
        parser.error("--sample must be at least 1")
    try:
        report = run(args.check, args.state_dir.resolve(), args.cycle,
                     budget_seconds=args.budget_seconds, sample=args.sample,
                     books=args.bc_books, previous_report=args.previous_report,
                     environ=environ, sender=sender, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 - reported as a class name only
        print(json.dumps({"probe": PROBE, "check": args.check, "error": type(exc).__name__}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
