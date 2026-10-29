"""Read-only live probes with no adapter CLI of their own (counts/shapes only).

Run by the credentialed live verifier on reviewed ``main`` through the specs in
``devloop/live-checks/`` (``python3 devloop/live_verify.py``)::

    python3 -m scripts.live_read_probes portal-health
    python3 -m scripts.live_read_probes vmc-search
    python3 -m scripts.live_read_probes mc-tools
    python3 -m scripts.live_read_probes mirror-full-coverage --db DIR/mirror.sqlite \
        [--budget-seconds N] [--resolve-sample 5000] [--previous-report DAILY.json] \
        [--workers 1..4]
    python3 -m scripts.live_read_probes schema-drift
    python3 -m scripts.live_read_probes schema-queryable [--rows 100] [--budget-seconds N]
    python3 -m scripts.live_read_probes mirror-fallback [--state-dir DIR] \
        [--budget-seconds N] [--sample 20] [--workers 1..4] [--mirror-ref REF]
    python3 -m scripts.live_read_probes identity-spine --session-projection DIR

Every request goes through the existing guarded clients, never a new HTTP path:
``integrations.workbench_read_api`` (Portal reporting API, GET only; the
``mirror-full-coverage`` probe drives it through ``integrations.source_mirror``
into a throwaway mirror file, bound relations first, and reports counts, shapes and
timings only),
``integrations.vmc.BrainReadClient`` (the brain's agent GET routes, checked by
``plx_hub_guard.check_brain_request``) and ``integrations.mc.MissionControlMcpClient``
(``initialize`` and ``tools/list`` only; no ``tools/call``, so no MC audit row and
no action). ``schema-drift`` reads only ``/v1/relations`` (the catalog) and diffs it
against the committed ontology schema (``scripts/gen_ontology_skeleton.drift_live``).
``schema-queryable`` reads one keyset page of every Portal relation the compiled schema
binds, projects those rows into a throwaway store under that schema, and reads every
binding back through ``plx.find``/``plx.object`` (``scripts/source_registry.probe_binding``,
the probe the fixture stage uses, here on live rows) as a principal granted every
audience and marking, while one granted nothing must get no candidate; it names the
relations that came back, those read empty and those that failed (each relation has its
own outcome under one total deadline), never a row value.
``mirror-fallback`` syncs the Portal relations ``plx.find`` reads into the capture
store's working mirror outside Git (``integrations/mirror_capture.py``), persists the
copy when complete and, when the API does not answer, serves the last complete copy
labelled stale; then ``plx.find`` over identifiers drawn from the served mirror, as the
probe principal and as one the audience withholds. Output holds status flags, counts, key names and MC tool names;
never secrets, hosts' keys, query hits, titles, snippets or rows. ``portal-health`` also
names the relations the API does not serve with the upstream reason as plain text, and a
failed probe's stderr carries the sanitized ``reason`` of a Portal readiness or relation
failure (``integrations.workbench_read_api.failure_reason``).
``identity-spine`` reads no source: it opens the session's committed projection
generation read-only and prints ``ontology.identity_spine.summarize`` (counts only).

Environment (names only): ``PLX_WORKBENCH_API_URL`` + ``PLX_WORKBENCH_API_KEY``;
``VMC_API_URL`` + ``VMC_API_KEY`` (or ``BRAIN_BASE_URL`` + ``BRAIN_API_KEY``);
``MC_MCP_API_KEY`` + ``MC_OPERATOR_EMAIL`` (optional ``MC_BASE_URL``/``MC_REPO``).

``mc-tools --operator-email-default <address>``: the ``x-mc-operator-email``
admission value used only when ``MC_OPERATOR_EMAIL`` is unset (the variable
always wins). The address lives in the reviewed spec
(``devloop/live-checks/mc.initialize_tools_list.json``), never in this code;
the report says which one was used (``operator_email_source``), never the value.
It also compares every listed tool's ``inputSchema`` (property and required names) and
``outputSchema`` with the source-derived pin ``ontology/schema/generated/meta/mcp_tools.json``
(``mc_schema_diff``: tool names only).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
# Fixed and harmless; the brain search result is reported as counts only.
VMC_PROBE_QUERY = "PLX Agent Workbench"
MCP_PROTOCOL_VERSION = "2025-06-18"


def portal_health(environ: Mapping[str, str], *, client: Any = None) -> dict[str, Any]:
    """``GET /v1/health`` and the relation catalog: readiness and counts.

    A degraded API (relations its health or catalog names unavailable, the rest served)
    reports ``degraded`` with ``unavailable_relations`` ({relation: upstream reason}) and
    one ``blocked_upstream`` part per relation (cause ``relation_unavailable``); the
    served rest is still counted. ``warnings`` carries the early warnings (login expiring
    soon with its expiry and days left, the count of API keys expiring soon), which never
    fail the probe. Health that fails closed raises (exit 1, the reason on stderr)."""
    from integrations.workbench_read_api import RelationUnavailable, served_entry
    if client is None:
        base_url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
        if not base_url or not key:
            raise LookupError("missing environment variables: PLX_WORKBENCH_API_URL, "
                              "PLX_WORKBENCH_API_KEY")
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(base_url, key)
    health = client.health()  # WorkbenchNotReady unless ready or degraded
    relations = client.relations()
    listed = health.get("unavailable_relations") or {}
    unavailable: dict[str, str] = {}
    for name in sorted({*relations, *listed}):
        try:
            served_entry(relations, name, listed)
        except RelationUnavailable as exc:
            unavailable[name] = exc.reason
    schemas: dict[str, int] = {}
    for name in relations:
        schema = name.split(".", 1)[0] if "." in name else "?"
        schemas[schema] = schemas.get(schema, 0) + 1
    # A relation declared waiting on a later_ask names it, so the blocked part is not silent.
    from ontology.schema import load_upstream_asks
    asks = load_upstream_asks()
    unavailable = {name: f"later_ask {asks[name]}: {reason}" if name in asks else reason
                   for name, reason in unavailable.items()}
    lag = health.get("replayLagSeconds")
    return {"probe": "portal-health/v1", "health_ok": health.get("ok") is True,
            "recovery": health.get("recovery") is True, "degraded": bool(unavailable),
            "warnings": dict(health.get("warnings") or {}),
            "replay_lag_seconds": lag if isinstance(lag, (int, float)) else None,
            "unavailable_count": len(health.get("unavailable") or []),
            "missing_columns_count": len(health.get("missingColumns") or []),
            "unavailable_relations": unavailable,
            "unavailable_relation_count": len(unavailable),
            "available_relation_count": sum(1 for name in relations if name not in unavailable),
            "relation_count": len(relations), "relations_by_schema": schemas,
            "relations_with_since": sum(1 for entry in relations.values()
                                        if isinstance(entry, Mapping) and entry.get("since")),
            "blocked_upstream": [{"part": name, "source": "portal-workbench-api",
                                  "cause": "relation_unavailable", "reason": reason}
                                 for name, reason in unavailable.items()]}


VMC_DEGRADED_FLAG = re.compile(r"([a-z][A-Za-z0-9]{0,31})Degraded")


def vmc_degraded(data: Mapping[str, Any]) -> list[str]:
    """Components a search response flags degraded (``semanticDegraded`` -> ``semantic``).

    Only the flag's key names the component, never response text; a flag that is not
    plainly ``False`` (true, null, a string) counts as degraded, so an odd value is
    never read as healthy."""
    return sorted(match.group(1) for key, value in data.items()
                  if isinstance(key, str) and (match := VMC_DEGRADED_FLAG.fullmatch(key))
                  and value is not False)


def vmc_search(environ: Mapping[str, str], *, client: Any = None) -> dict[str, Any]:
    """One brain ``agent/search`` GET with a fixed query; counts and key names only.

    Each component the response flags degraded is an upstream state, not a Workbench
    failure: it is listed in ``blocked_upstream`` (error code ``upstream_degraded``), so the
    live check is blocked naming it; a partial answer with no such flag still fails."""
    report: dict[str, Any] = {"probe": "vmc-search/v1"}
    if client is None:
        from integrations.plx_hub_guard import BrainConfig, HubTransport
        from integrations.vmc import BrainReadClient
        hub = HubTransport(environ=environ)
        config = BrainConfig.from_env(environ)
        client = BrainReadClient(config, hub=hub)
        report["pair"] = config.pair
    response = client.search(VMC_PROBE_QUERY, limit=3)
    data = response["data"]
    results = data.get("results") or []
    degraded = vmc_degraded(data)
    report.update({"top_keys": sorted(str(k) for k in response),
                   "data_keys": sorted(str(k) for k in data),
                   "result_count": len(results), "total": data.get("total"),
                   "partial": data.get("partial"),
                   "degraded": degraded,
                   "blocked_upstream": [{"part": name, "source": "vmc",
                                         "error_code": "upstream_degraded",
                                         "cause": "upstream_outage"} for name in degraded],
                   "result_keys": sorted({str(k) for r in results if isinstance(r, Mapping)
                                          for k in r}),
                   "trust_label": (response.get("trust") or {}).get("label")})
    return report


def operator_email_source(environ: Mapping[str, str], default: str | None) -> str:
    if (environ.get("MC_OPERATOR_EMAIL") or "").strip():
        return "env"
    return "spec_default" if default and default.strip() else "missing"


def mc_tools(environ: Mapping[str, str], *, client: Any = None,
             operator_email_default: str | None = None) -> dict[str, Any]:
    """MC MCP ``initialize`` + ``tools/list``: server name, protocol, tool names."""
    source = operator_email_source(environ, operator_email_default)
    if client is None:
        from integrations.mc import MissionControlMcpClient
        from integrations.plx_hub_guard import HubTransport, McConfig
        override = operator_email_default if source == "spec_default" else None
        client = MissionControlMcpClient(McConfig.from_env(environ, operator_email=override),
                                         hub=HubTransport(environ=environ))
    # MissionControlMcpClient has no public initialize(); its _rpc runs the same
    # read-only guard (check_mcp_body admits initialize/tools/list/read tools only).
    init = client._rpc("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION,
                                      "capabilities": {},
                                      "clientInfo": {"name": "plx-workbench-live-verify",
                                                     "version": "1"}})
    server = init.get("serverInfo") if isinstance(init.get("serverInfo"), Mapping) else {}
    specs = client.list_tool_specs()
    names = [t["name"] for t in specs]
    return {"probe": "mc-tools/v1", "initialize_ok": True, "operator_email_source": source,
            "server_name": server.get("name") if isinstance(server.get("name"), str) else None,
            "protocol": init.get("protocolVersion")
            if isinstance(init.get("protocolVersion"), str) else None,
            "tool_count": len(names), "tool_names": names, **mc_schema_diff(specs)}


def _live_input(spec: Mapping[str, Any]) -> tuple[list[str], list[str]] | None:
    """(property names, required names) of a tools/list ``inputSchema``; None when it is
    not an object schema of that form."""
    schema = spec.get("inputSchema")
    if not isinstance(schema, Mapping) or schema.get("type", "object") != "object":
        return None
    props, required = schema.get("properties", {}), schema.get("required", [])
    if not isinstance(props, Mapping) or not isinstance(required, list) or \
            not all(isinstance(r, str) for r in required):
        return None
    return sorted(props), sorted(required)


def mc_schema_diff(specs: Sequence[Mapping[str, Any]], pin_path: Path | None = None
                   ) -> dict[str, Any]:
    """The live ``tools/list`` against the source-derived pin
    (``ontology/schema/generated/meta/mcp_tools.json``): ``schema_mismatches`` (input
    property names or required names differ, an unreadable input schema, or an output
    schema the pin does not declare), ``schema_unpinned`` (a live tool the pin lacks),
    ``schema_missing`` (a pinned HTTP tool MC did not list: an MC allowlist may hide it),
    ``output_schema_tools``. Tool names only."""
    from ontology.generator.mcp import PIN_PATH
    pin = json.loads((pin_path or PIN_PATH).read_text(encoding="utf-8"))["servers"]["plx-mc"]
    pinned = pin["tools"]
    policy = json.loads((Path(__file__).resolve().parents[1] / "integrations" /
                         "mc_tool_policy.json").read_text(encoding="utf-8"))
    mismatched, unpinned, output = [], [], []
    for spec in specs:
        name, want = spec["name"], pinned.get(spec["name"])
        declares_output = isinstance(spec.get("outputSchema"), Mapping)
        if declares_output:
            output.append(name)
        if want is None:
            unpinned.append(name)
            continue
        inputs = want["input"]
        expected = None if inputs is None else (
            sorted(inputs), sorted(k for k, p in inputs.items() if p["required"]))
        if expected is None or _live_input(spec) != expected or \
                declares_output != (want["output_schema"] == "declared"):
            mismatched.append(name)
    listed = {s["name"] for s in specs}
    served = set(pinned) - set(policy["stdio_only"])
    return {"schema_matched": len(specs) - len(mismatched) - len(unpinned),
            "schema_mismatches": sorted(mismatched), "schema_unpinned": sorted(unpinned),
            "schema_missing": sorted(served - listed), "output_schema_tools": sorted(output)}


def mirror_full_coverage(environ: Mapping[str, str], *, client: Any = None,
                         db: str | None = None, budget_seconds: float | None = None,
                         resolve_sample: int = 5000,
                         previous_report: str | None = None,
                         workers: int = 1) -> dict[str, Any]:
    """Full mirror sync of every served relation, then a batch resolve (counts only).

    Relations bound in ``ontology/schema/domains`` are captured first and reported as
    ``complete_bound_relation_count`` of ``bound_total`` (``scripts.mirror_measure``).
    The relations ``previous_report`` (the last daily verifier report) names unstable
    are never staged: they are counted key-only and leave ``bound_total``; the ones it
    names row-bound or paging-defective are synced last. ``workers`` relations are read
    at a time (1..4, inside the service's five ``wb_api`` connections)."""
    if not db:
        raise LookupError("mirror-full-coverage needs --db")
    if client is None:
        base_url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
        if not base_url or not key:
            raise LookupError("missing environment variables: PLX_WORKBENCH_API_URL, "
                              "PLX_WORKBENCH_API_KEY")
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(base_url, key)
    from scripts.mirror_measure import (ordered_full_coverage, previous_deferred,
                                        previous_unstable)
    return ordered_full_coverage(client, db, budget_seconds=budget_seconds,
                                 resolve_sample=resolve_sample,
                                 unstable=previous_unstable(previous_report),
                                 deferred=previous_deferred(previous_report),
                                 workers=workers)


def schema_drift(environ: Mapping[str, str], *, client: Any = None) -> dict[str, Any]:
    """Ontology schema drift against ``GET /v1/relations`` (names, digests, counts)."""
    from scripts.gen_ontology_skeleton import drift_live
    return drift_live(environ, client=client)


QUERYABLE_PRINCIPAL = "service:schema-queryable-probe"
QUERYABLE_WITHHELD = "service:schema-queryable-withheld"
QUERYABLE_ROWS = 100   # x bindings per type stays inside probe_binding's 20 x 50 find pages
GROUP_SIZE = 50   # devloop/live_verify.py MAX_ITEMS: a longer recorded list is only a count
# The reads' deadline: the catalog read plus 104 relations x a 40 s read (the client's
# timeout) was 4200 s worst case (Portal 52c9116). The catalog pinned from the 2026-10-26
# live read binds 248: 9,920 s if every read times out, past this deadline, so such a run
# counts the rest unreached and is incomplete, never a pass (no timing of 248 live reads
# is recorded yet: measure before moving the deadline). Pages already read are still projected and
# probed for up to the grace after it (seconds for 104 x 100 rows), so a slow read phase never
# wastes what it read; the daily spec's 5400 s timeout covers budget + one read in flight +
# grace.
QUERYABLE_BUDGET_SECONDS = 4800.0
QUERYABLE_GRACE_SECONDS = 300.0
# The spec declares the floor (expect.json_min.queryable_share: the share of bound relations
# that must come back) and what a complete run is (complete_when); the probe reads both, so
# neither is declared twice: devloop/DECISIONS/2026-10-07-portal-queryable-floor.md.
QUERYABLE_SPEC = Path(__file__).resolve().parents[1] / "devloop" / "live-checks" / \
    "schema.portal_queryable.json"


def _queryable_terms(path: Path) -> tuple[Decimal, Mapping[str, Any]]:
    """The floor (an exact ``Decimal`` in (0, 1]) and ``complete_when`` of the spec at
    ``path``; a missing or malformed declaration raises, so the run fails closed."""
    doc = json.loads(path.read_text("utf-8"), parse_float=Decimal)
    floor = ((doc.get("expect") or {}).get("json_min") or {}).get("queryable_share")
    complete_when = doc.get("complete_when")
    if isinstance(floor, bool) or not isinstance(floor, (int, Decimal)) or \
            not 0 < floor <= 1:
        raise ValueError("schema.portal_queryable: expect.json_min.queryable_share must be "
                         "a number in (0, 1]")
    if not isinstance(complete_when, dict) or not complete_when:
        raise ValueError("schema.portal_queryable: complete_when must be a non-empty object")
    return Decimal(floor), complete_when


class _LivePages:
    """A ``portal_api`` capture source over the rows of one live page per relation,
    cited as the source mirror cites them (``make_citation``) under a probe-only
    generation; held in memory for one throwaway store."""

    source = "portal_api"

    def __init__(self, pages: Mapping[str, tuple[Sequence[str], list[dict[str, Any]]]],
                 at: str, generation: str) -> None:
        self.pages, self.at, self.generation = pages, at, generation

    def records(self, relations: Sequence[str]) -> Mapping[str, Any]:
        import hashlib
        from integrations.source_mirror import make_citation
        from ontology.project import CapturedRecord, _Relations

        def stream(relation: str) -> Any:
            key_columns, rows = self.pages.get(relation, ((), []))
            runs: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                runs.setdefault(json.dumps({c: row[c] for c in key_columns}, sort_keys=True,
                                           separators=(",", ":")), []).append(row)
            for key_json, run in runs.items():
                for index, row in enumerate(run):
                    text = json.dumps(row, sort_keys=True, separators=(",", ":"))
                    yield CapturedRecord(
                        self.source, relation, key_json + (f"#{index}" if len(run) > 1 else ""),
                        row, make_citation(relation, json.loads(key_json), 1, self.generation),
                        f"rows.{index}" if len(run) > 1 else "row",
                        hashlib.sha256(text.encode("utf-8")).hexdigest(), self.at, None,
                        self.at, f"api:{relation}")

        return _Relations(relations, stream)

    def describe(self) -> dict[str, Any]:
        return {"generation": self.generation, "relations": {}}


def _named_groups(names: Sequence[str]) -> dict[str, list[str]]:
    """Relation names in groups of at most ``GROUP_SIZE`` (a Portal table by its bare
    name, as the source registry also matches it), keyed ``<schema>[_app|_fm][_<n>]``."""
    groups: dict[str, list[str]] = {}
    for name in sorted(names):
        schema, table = name.split(".", 1)
        if schema == "public":
            group, shown = ("public_app" if table.startswith("app_") else "public_fm"), table
        else:
            group, shown = schema, name
        groups.setdefault(group, []).append(shown)
    out: dict[str, list[str]] = {}
    for group, members in groups.items():
        for start in range(0, len(members), GROUP_SIZE):
            n = start // GROUP_SIZE
            out[group + (f"_{n + 1}" if n else "")] = members[start:start + GROUP_SIZE]
    return out


def schema_queryable(environ: Mapping[str, str], *, client: Any = None,
                     rows: int = QUERYABLE_ROWS,
                     budget_seconds: float = QUERYABLE_BUDGET_SECONDS,
                     clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Every Portal relation the compiled schema binds, live: one page read, projected
    and read back through the ontology tools; names and counts only.

    Each relation gets its own outcome; one that fails never stops the others.
    ``queryable``: relations with rows where every sampled binding came back and none
    failed (a binding whose ``where`` matched no row of the page is ``unsampled``, not a
    failure); ``read_empty``: served relations whose page had no row (read live, nothing
    projected or read back: bound but unexercised, never queryable). Both are named,
    grouped by ``_named_groups``. ``not_queryable``: the failing relations, grouped the
    same way (names only, so the list fits the verifier's detail cap whatever the count);
    ``not_queryable_codes``: failure code -> relations failing with it (``read_<class>``,
    ``project_<class>`` when its rows alone break the projection, ``probe_<class>`` or
    ``probe_binding``'s code).
    ``unavailable``: bound relations the live catalog lists but does not serve (marked
    unavailable or unpaged there, or their rows route answered ``relation-unavailable``),
    grouped the same way, each also one ``blocked_upstream`` part (cause
    ``relation_unavailable``, the catalog's reason) so the verifier records the run
    ``blocked`` naming them, never ``pass``. A bound relation absent from the catalog is
    catalog drift (as ``schema.catalog_drift`` reports it): ``not_queryable`` with code
    ``catalog_absent``, a failure; ``unreached_count``: relations not evaluated in time: not read before ``budget_seconds``
    ran out, or read but not projected or probed within ``QUERYABLE_GRACE_SECONDS`` after
    it (together the total deadline). Neither is
    named: nothing about them was evaluated. ``denied_leaks``: candidates a principal
    granted nothing got. ``queryable_share``: queryable over bound relations (the catalog the
    schema binds: read_empty, unsampled, unavailable, unreached and failing ones all count
    against it), rounded down to 4 places (null when nothing is bound). ``ok``: the run is
    complete (the spec's ``complete_when``), the share reaches the spec's floor (compared
    exactly) and nothing leaked; a failing relation is named and costs only its share, and
    an incomplete run never passes. ``served_ok``: the same terms with unavailable relations
    left out of completeness and out of the share the floor is measured on (the reported
    ``queryable_share`` still counts them against it), so a run whose only shortfall is
    relations unavailable upstream, a floor breach included, is told apart from one that
    also failed."""
    label = "injected-transport" if client is not None else "live-read"
    if not 1 <= rows <= 5000:
        raise ValueError("schema-queryable --rows must be 1..5000")
    if not 0 < budget_seconds <= 7200:
        raise ValueError("schema-queryable --budget-seconds must be in (0, 7200]")
    floor, complete_when = _queryable_terms(QUERYABLE_SPEC)
    deadline = clock() + budget_seconds
    stop = deadline + QUERYABLE_GRACE_SECONDS
    if client is None:
        base_url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
        if not base_url or not key:
            raise LookupError("missing environment variables: PLX_WORKBENCH_API_URL, "
                              "PLX_WORKBENCH_API_KEY")
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(base_url, key)
    import hashlib
    from integrations.workbench_read_api import RelationUnavailable, served_entry
    from ontology import identifiers, store_ddl
    from ontology.core import Actor
    from ontology.project import build_store
    from ontology.schema import load_schema
    from operating import ontology_tools as ot
    from scripts.source_registry import probe_binding

    schema = load_schema()
    bound = schema.portal_bindings()
    catalog = client.relations()
    now = datetime.now(timezone.utc)
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)   # tools read at ms
    at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    generation = hashlib.sha256(at.encode()).hexdigest()[:16]
    pages: dict[str, tuple[Sequence[str], list[dict[str, Any]]]] = {}
    unavailable: dict[str, str] = {}
    unreached: list[str] = []
    not_queryable: dict[str, str] = {}
    for name in sorted(bound):
        if clock() >= deadline:
            unreached.append(name)
            continue
        if name not in catalog:
            not_queryable[name] = "catalog_absent"
            continue
        try:
            entry = served_entry(catalog, name)
            page = client.read_page(name, entry, columns=list(entry["columns"]),
                                    page_size=rows)
        except RelationUnavailable as exc:
            unavailable[name] = exc.reason
            continue
        except Exception as exc:  # noqa: BLE001 - the class only; messages may carry values
            not_queryable[name] = "read_" + type(exc).__name__
            continue
        pages[name] = (list(entry["key"]), page["rows"])
    empty = sorted(name for name, (_, page_rows) in pages.items() if not page_rows)
    filled = sorted(name for name, (_, page_rows) in pages.items() if page_rows)
    who, nobody = frozenset({QUERYABLE_PRINCIPAL}), frozenset({QUERYABLE_WITHHELD})
    actor, withheld = Actor("plx", who), Actor("plx", nobody)
    queryable, unsampled = [], []
    leaks = probed = 0
    with tempfile.TemporaryDirectory(prefix="plx-schema-queryable-") as work:
        path = Path(work) / "store.sqlite"

        def build(store: Path, names: Sequence[str]) -> None:
            build_store(store, schema, {"portal_api": _LivePages(
                {name: pages[name] for name in names}, at, generation)},
                normalizer=identifiers.normalize, clock=lambda: now)

        try:
            build(path, filled)
        except Exception:  # noqa: BLE001 - isolate: each relation's rows alone, then the rest
            isolated = []
            for index, name in enumerate(filled):
                if clock() >= stop:
                    unreached.append(name)
                    continue
                try:
                    build(Path(work) / f"alone-{index}.sqlite", [name])
                except Exception as exc:  # noqa: BLE001
                    not_queryable[name] = "project_" + type(exc).__name__
                    continue
                isolated.append(name)
            filled = isolated
            try:
                build(path, filled)
            except Exception as exc:  # noqa: BLE001 - they project alone, not together
                not_queryable.update((name, "project_" + type(exc).__name__)
                                     for name in filled)
                filled = []
        if filled:
            reader = ot.StoreReader.open(path)
            try:
                policy = ot.ReadPolicy(tenant="plx",
                                       audiences={k: who for k in reader.audience_keys()},
                                       markings={m: who for m in store_ddl.MARKINGS})
                tools = ot.OntologyTools(reader, policy,
                                         schema=ot.SchemaView.from_compiled(schema),
                                         grammar=ot.default_grammar(), clock=lambda: now)
                for name in filled:
                    if clock() >= stop:
                        unreached.append(name)
                        continue
                    outcomes = []
                    try:
                        for type_id, binding in sorted(bound[name],
                                                       key=lambda tb: tb[1]["id"]):
                            spec = schema.object_types[type_id]
                            why = probe_binding(tools, actor, spec, binding["id"])
                            if why == "not_found" and binding.get("where"):
                                why = "unsampled"
                            outcomes.append(why)
                            if why is None:
                                probed += 1
                                prop = next(p["name"] for p in spec["properties"]
                                            if any(src["binding"] == binding["id"]
                                                   for src in p["sources"]))
                                hidden = tools.call("plx.find", {
                                    "object_type": type_id, "limit": 50,
                                    "filters": [{"property": prop, "op": "present"}]},
                                    withheld)
                                leaks += len(hidden.get("candidates") or ())
                    except Exception as exc:  # noqa: BLE001 - the class only
                        not_queryable[name] = "probe_" + type(exc).__name__
                        continue
                    failed = [why for why in outcomes if why not in (None, "unsampled")]
                    if failed:
                        not_queryable[name] = failed[0]
                    elif None in outcomes:
                        queryable.append(name)
                    else:
                        unsampled.append(name)
            finally:
                reader.conn.close()
    named = set(queryable) | set(empty) | set(not_queryable)
    share = (Decimal(len(queryable)) / len(bound)).quantize(Decimal("0.0001"), ROUND_DOWN) \
        if bound else None
    codes: dict[str, int] = {}
    for code in not_queryable.values():
        codes[code] = codes.get(code, 0) + 1
    report = {"probe": "schema-portal-queryable/v1", "evidence_label": label,
              "rows_per_relation": rows, "budget_seconds": budget_seconds,
              "bound_relation_count": len(bound),
              "queryable_count": len(queryable), "read_empty_count": len(empty),
              "unsampled_count": len(unsampled), "unavailable_count": len(unavailable),
              "unreached_count": len(unreached), "not_queryable_count": len(not_queryable),
              "named_relation_count": len(named),
              "queryable": _named_groups(queryable), "read_empty": _named_groups(empty),
              "not_queryable": _named_groups(not_queryable),
              "not_queryable_codes": dict(sorted(codes.items())),
              "unavailable": _named_groups(unavailable),
              "blocked_upstream": [{"part": name, "source": "portal-workbench-api",
                                    "cause": "relation_unavailable", "reason": reason}
                                   for name, reason in sorted(unavailable.items())],
              "queryable_share": None if share is None else float(share),
              "queryable_floor": float(floor),
              "bindings_probed": probed, "denied_leaks": leaks}
    served = len(bound) - len(unavailable)
    measured = bool(queryable) and len(queryable) >= floor * served and leaks == 0
    report["served_ok"] = measured and all(k in report and report[k] == v for k, v in
                                           complete_when.items() if k != "unavailable_count")
    report["ok"] = report["served_ok"] and all(k in report and report[k] == v
                                               for k, v in complete_when.items())
    return report


FALLBACK_PRINCIPAL = "service:mirror-fallback-probe"
FALLBACK_WITHHELD = "service:mirror-fallback-withheld"


def mirror_fallback(environ: Mapping[str, str], *, client: Any = None,
                    state_dir: str | None = None, budget_seconds: float | None = None,
                    sample: int = 20, workers: int = 1, branch: Any = None,
                    mirror_ref: str | None = None) -> dict[str, Any]:
    """``plx.find`` through a Portal API outage: live sync and persist when the API
    answers, the last complete persisted copy labelled stale when it does not.

    ``blocked`` (``never_synced_new_store`` / ``never_synced``): the source is out and
    the store's history shows no complete copy of the probe's relations was ever
    published in it (on its first run, or on a state directory that does not survive
    between runs; or across runs that all met the outage). ``answered``: at least one
    find returned candidates; ``label_ok``: every such find
    states ``portal_api`` as ``served_from: persisted_capture``, stale, with the
    persisted-capture label as its first gap when the copy was served, and never a
    persisted capture when live; ``permission_trim_ok``: the withheld
    principal got no candidate. Counts, booleans and enums only.

    ``mirror_ref`` (owner policy e251073ef3c5 item 6): when the store holds no copy
    of these relations and the source is out, the copy the verifier published on that
    ref of this repository and attested on claude/devloop-live
    (``integrations/mirror_branch_store.py``) is installed into the store and served the
    same way; ``branch_status`` says what the ref gave, ``branch_reason`` why it was
    refused (``unattested``, a digest, ...). Only with the live client: an injected
    client (a fixture) reads only an injected ``branch``, so a test never reads the
    repository's real ref.

    ``blocked`` ``state_dir_unset`` (``missing_settings`` names
    ``PLX_WORKBENCH_STATE_DIR``): neither that setting nor ``--state-dir`` names the
    durable store, so nothing is read; a per-run directory could never show the fallback."""
    from integrations import mirror_capture
    configured = mirror_capture.default_state_dir(environ)
    if configured is None and not state_dir:
        return {"probe": "mirror-fallback/v1", "blocked": "state_dir_unset",
                "missing_settings": [mirror_capture.STATE_ENV],
                "evidence_label": "live-read", "adapter_status": "live-unverified"}
    if client is None and mirror_ref and branch is None:
        from integrations.mirror_branch_store import MirrorBranch
        branch = MirrorBranch(ROOT, ref=mirror_ref)
    if client is None:
        base_url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
        if not base_url or not key:
            raise LookupError("missing environment variables: PLX_WORKBENCH_API_URL, "
                              "PLX_WORKBENCH_API_KEY")
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(base_url, key)
    from integrations.source_mirror import (AudienceMap, MirrorError, load_ingest_config,
                                            mirror_options)
    from ontology import identifiers, resolve
    from ontology.core import Actor
    from ontology.project import build_store
    from ontology.schema import load_schema
    from operating import ontology_tools as ot

    schema = load_schema()
    kinds = {prop.get("identifier_kind") for spec in schema.object_types.values()
             for prop in spec.get("properties", ())
             if prop.get("value_type") == "identifier"} - {None}
    carriers = [c for c in resolve.carriers(schema, kinds) if c.source == "portal_api"]
    relations = sorted({c.relation for c in carriers})
    actor = Actor("plx", frozenset({FALLBACK_PRINCIPAL}), "restricted")
    withheld = Actor("plx", frozenset({FALLBACK_WITHHELD}), "restricted")
    audience = AudienceMap({name: [FALLBACK_PRINCIPAL] for name in relations}, tenant="plx")
    # The explicitly configured store wins over --state-dir.
    store = mirror_capture.MirrorCaptureStore(configured or state_dir)
    before = store.note_run()
    # Persistence between runs is what the store's own history shows, never assumed
    # from a configured path (a fresh container may discard any directory).
    out: dict[str, Any] = {"probe": "mirror-fallback/v1", "relation_count": len(relations),
                           "store_persists_between_runs": before["runs"] > 0,
                           "store_runs_seen": before["runs"] + 1,
                           "store_persisted_count": before["persisted"],
                           "evidence_label": "live-read", "adapter_status": "live-unverified",
                           "branch_status": "not_configured" if branch is None else
                           "not_needed"}

    def serve() -> tuple[Any, dict[str, Any]]:
        return mirror_capture.sync_or_persisted(
            store, client, audience, actor, relations=relations,
            budget_seconds=budget_seconds, workers=workers,
            **mirror_options(load_ingest_config()))
    try:
        try:
            mirror, served = serve()
        except MirrorError as exc:
            if branch is None or exc.code not in ("missing_mirror", "persisted_incomplete"):
                raise
            restored = branch.restore_into(store, relations)
            out.update(branch_status=restored["status"],
                       branch_source=restored["branch_source"],
                       **({"branch_reason": restored["reason"]} if "reason" in restored
                          else {}))
            if restored["status"] != "restored":
                raise
            mirror, served = serve()
    except MirrorError as exc:
        out.update(answered=False, served_from="none", error_code=exc.code)
        # A branch copy that did not verify is a failure, never "nothing to serve yet".
        if exc.code == "missing_mirror" and out["branch_status"] != "refused" and \
                mirror_capture.scope_id(relations) not in before["persisted_scopes"]:
            # The source is out and this store never published a complete copy of
            # these relations (another scope, e.g. the host's, cannot answer them):
            # nothing exists to fall back to yet. Blocked on the upstream, not a pass;
            # a copy published here earlier and missing now stays a failure.
            out["blocked"] = "never_synced" if before["runs"] else "never_synced_new_store"
        return out
    freshness = served.get("source_freshness")
    out.update(store_persisted_count=store.history()["persisted"],
               served_from=served["served_from"], complete=served["complete"],
               persisted=bool(served["persisted"].get("persisted")),
               persist_reason=served["persisted"].get("reason"),
               captured_count=served["captured_count"],
               http_status=freshness["http_status"] if freshness else None,
               upstream_error_code=freshness["error_code"] if freshness else None,
               never_live_when_failed=(freshness is None or freshness["live"] is False))
    with tempfile.TemporaryDirectory(prefix="plx-mirror-fallback-") as work:
        values: list[tuple[str, str]] = []
        per_carrier = max(1, -(-sample // max(1, len(carriers))))
        unreadable = 0
        for carrier in carriers:
            if "." in carrier.path or len(values) >= sample:
                continue
            try:
                page = mirror.rows(actor, carrier.relation, columns=[carrier.path],
                                   where=[[carrier.path, "is_null", False]],
                                   limit=per_carrier)
            except MirrorError:
                unreadable += 1
                continue
            values.extend((carrier.kind, str(item["row"][carrier.path]).strip())
                          for item in page["rows"] if str(item["row"][carrier.path]).strip())
        values = values[:sample]
        empty = Path(work) / "empty.sqlite"
        build_store(empty, schema, {}, normalizer=identifiers.normalize)
        reader = ot.StoreReader.open(empty)
        view = ot.SchemaView.from_compiled(schema)
        resolver = resolve.SourceResolver(view, [resolve.MirrorLookup(mirror)],
                                          normalizer=identifiers.normalize,
                                          workdir=Path(work) / "live")
        try:
            tools = ot.OntologyTools(
                reader, ot.ReadPolicy("plx", audiences={
                    f"api:{name}": {FALLBACK_PRINCIPAL} for name in relations}),
                schema=view, grammar=identifiers, sources=resolver)
            answered = labelled = trimmed_leaks = errors = 0
            label = freshness["label"] if freshness else None
            for kind, value in values:
                arguments = {"query": value, "identifier_kind": kind}
                result = tools.call("plx.find", arguments, actor)
                if "error" in result:
                    errors += 1
                    continue
                if result["candidates"]:
                    answered += 1
                    portal = [f for f in result["meta"]["freshness"]
                              if f["source"] == "portal_api"]
                    served = [(f.get("served_from"), f["stale"], f.get("label"))
                              for f in portal]
                    first = (result["gaps"] or [{}])[0]
                    labelled += ((served == [("persisted_capture", True, label)]
                                  and first.get("detail") == label) if label else
                                 all(item[0] != "persisted_capture" and item[2] is None
                                     for item in served))
                hidden = tools.call("plx.find", arguments, withheld)
                trimmed_leaks += len(hidden.get("candidates") or ())
        finally:
            reader.conn.close()
            resolver.close()
            mirror.close()
    out.update(finds=len(values), finds_with_candidates=answered, finds_labelled=labelled,
               find_errors=errors, carriers_unreadable=unreadable,
               answered=answered > 0, label_ok=answered > 0 and labelled == answered,
               permission_trim_ok=trimmed_leaks == 0, withheld_candidates=trimmed_leaks)
    if freshness:
        as_of = datetime.fromisoformat(freshness["captures_as_of"].replace("Z", "+00:00"))
        out["captures_age_hours"] = round(
            (datetime.now(timezone.utc) - as_of).total_seconds() / 3600, 2)
    text = json.dumps(out, sort_keys=True)
    out["row_data_leaked"] = sum(1 for _, value in values if len(value) >= 8 and value in text)
    return out


SPINE_SOURCES = ("bc", "portal_api")


def identity_spine(environ: Mapping[str, str], *, client: Any = None,
                   session_projection: str | None = None) -> dict[str, Any]:
    """``ontology.identity_spine``: the identity-spine counts (``ontology.identity_spine``)
    of the projection generation this verifier session committed, opened read-only
    (``scripts.ontology_projection_probe.open_session``); nothing is read from a source.
    Counts only. No generation of this session: ``error``
    ``session_projection_unavailable``; a generation without the Portal or BC names the
    missing ones in ``sources_blocked``."""
    import contextlib
    import sqlite3

    from integrations.bc_read import session_fresh_since
    from ontology import identity_spine as spine, schema
    from scripts.ontology_projection_probe import open_session

    del client
    opened = open_session(session_projection, session_fresh_since(environ))
    if opened is None:
        return {"probe": "identity-spine", "error": "session_projection_unavailable",
                "session_projection": {"opened": False}}
    manifest, generation = opened["manifest"], opened["generation"]
    sources = sorted(str(s) for s in manifest.get("sources", []))
    out: dict[str, Any] = {
        "probe": "identity-spine", "session_sources": sources,
        "blocked_upstream": sorted(str(s) for s in manifest.get("blocked_upstream", [])),
        "session_projection": {"opened": True, "mode": generation.mode}}
    missing = [s for s in SPINE_SOURCES if s not in sources]
    if missing:
        out["sources_blocked"] = missing
    store = Path(generation.store).resolve()
    with contextlib.closing(sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)) as conn:
        summary = spine.summarize(conn, schema.load_schema(), SPINE_SOURCES)
    out.update(summary, not_ingested_count=len(summary["not_ingested"]))
    return out


# ``plx_hub_guard.UpstreamError.code`` values the guard itself assigns (its docstring).
UPSTREAM_ERROR_KINDS = frozenset({"upstream_unreachable", "upstream_error", "auth_refused",
                                  "checkout_held", "response_bound"})

PROBES: dict[str, Callable[..., dict[str, Any]]] = {
    "portal-health": portal_health, "vmc-search": vmc_search, "mc-tools": mc_tools,
    "mirror-full-coverage": mirror_full_coverage, "schema-drift": schema_drift,
    "mirror-fallback": mirror_fallback, "schema-queryable": schema_queryable,
    "identity-spine": identity_spine}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         clients: Mapping[str, Any] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.live_read_probes",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("probe", choices=sorted(PROBES))
    parser.add_argument("--operator-email-default", default=None,
                        help="mc-tools only: MC admission email when MC_OPERATOR_EMAIL is "
                             "unset (from the reviewed spec; never printed)")
    parser.add_argument("--db", help="mirror-full-coverage only: throwaway mirror file")
    parser.add_argument("--budget-seconds", type=float,
                        help="mirror-full-coverage, mirror-fallback: sync time budget; "
                             "schema-queryable: total deadline")
    parser.add_argument("--resolve-sample", type=int, default=5000,
                        help="mirror-full-coverage only: handles in the batch resolve")
    parser.add_argument("--previous-report", default=None,
                        help="mirror-full-coverage only: last daily verifier report "
                             "(its unstable relations are read key-only)")
    parser.add_argument("--workers", type=int, default=None,
                        help="mirror-full-coverage/mirror-fallback: relations read at a "
                             "time (1..4)")
    parser.add_argument("--state-dir", default=None,
                        help="mirror-fallback only: Workbench state directory outside Git, "
                             "used when PLX_WORKBENCH_STATE_DIR is not set")
    parser.add_argument("--mirror-ref", default=None,
                        help="mirror-fallback only: ref of this repository holding the "
                             "verifier's last complete copy (refs/devloop/mirror), read "
                             "when the store has none")
    parser.add_argument("--sample", type=int, default=None,
                        help="mirror-fallback only: identifiers asked of plx.find")
    parser.add_argument("--rows", type=int, default=None,
                        help="schema-queryable only: rows read per relation (one page)")
    parser.add_argument("--session-projection", default=None,
                        help="identity-spine only: the verifier session's projection "
                             "directory")
    args = parser.parse_args(argv)
    if (args.session_projection is None) != (args.probe != "identity-spine"):
        parser.error("--session-projection is required by, and applies to, identity-spine only")
    if args.operator_email_default is not None and args.probe != "mc-tools":
        parser.error("--operator-email-default applies to mc-tools only")
    if (args.db is not None or args.previous_report is not None) and \
            args.probe != "mirror-full-coverage":
        parser.error("--db and --previous-report apply to mirror-full-coverage only")
    if args.workers is not None and \
            args.probe not in ("mirror-full-coverage", "mirror-fallback"):
        parser.error("--workers applies to mirror-full-coverage and mirror-fallback only")
    if args.budget_seconds is not None and \
            args.probe not in ("mirror-full-coverage", "mirror-fallback", "schema-queryable"):
        parser.error("--budget-seconds applies to mirror-full-coverage, mirror-fallback and "
                     "schema-queryable only")
    if (args.state_dir is not None or args.sample is not None
            or args.mirror_ref is not None) and args.probe != "mirror-fallback":
        parser.error("--state-dir, --sample and --mirror-ref apply to mirror-fallback only")
    if args.rows is not None and args.probe != "schema-queryable":
        parser.error("--rows applies to schema-queryable only")
    env = os.environ if environ is None else environ
    client = (clients or {}).get(args.probe)
    kwargs: dict[str, Any] = ({"operator_email_default": args.operator_email_default}
                              if args.probe == "mc-tools" else {})
    if args.probe == "mirror-full-coverage":
        kwargs = {"db": args.db, "budget_seconds": args.budget_seconds,
                  "resolve_sample": args.resolve_sample,
                  "previous_report": args.previous_report,
                  "workers": 1 if args.workers is None else args.workers}
    if args.probe == "schema-queryable":
        kwargs = {"rows": QUERYABLE_ROWS if args.rows is None else args.rows,
                  "budget_seconds": (QUERYABLE_BUDGET_SECONDS if args.budget_seconds is None
                                     else args.budget_seconds)}
    if args.probe == "identity-spine":
        kwargs = {"session_projection": args.session_projection}
    if args.probe == "mirror-fallback":
        kwargs = {"state_dir": args.state_dir, "budget_seconds": args.budget_seconds,
                  "sample": 20 if args.sample is None else args.sample,
                  "workers": 1 if args.workers is None else args.workers,
                  "mirror_ref": args.mirror_ref,
                  "branch": (clients or {}).get("mirror-branch")}
    try:
        report = PROBES[args.probe](env, client=client, **kwargs)
    except Exception as exc:  # the class and a sanitized reason only; messages may carry URLs
        from devloop.data_profile import http_diagnostics, http_status_ok
        from integrations.live_source_guard import ReadOnlyGuardError
        from integrations.plx_hub_guard import UPSTREAM_CODES, UpstreamError
        from integrations.workbench_read_api import failure_reason
        code = "read_only_guard" if isinstance(exc, ReadOnlyGuardError) else type(exc).__name__
        reason = failure_reason(exc)
        diagnostics = http_diagnostics(exc)
        if isinstance(exc, UpstreamError):
            # MC/brain: the status and a closed-set code (the upstream code, else the
            # guard's own; an MC tool's free-text code reads "other") in the keys the
            # verifier publishes; never a message or body.
            upstream_code = exc.upstream_code or (
                exc.code if exc.code in UPSTREAM_ERROR_KINDS | UPSTREAM_CODES else "other")
            diagnostics = {**({"http_status": exc.status} if http_status_ok(exc.status) else {}),
                           "error_code": upstream_code}
        print(json.dumps({"error": code, "probe": args.probe, **diagnostics,
                          **({"reason": reason} if reason else {})}), file=sys.stderr)
        return 2 if code == "read_only_guard" else 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
