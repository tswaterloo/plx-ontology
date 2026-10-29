#!/usr/bin/env python3
"""PLX source registry (lane PT6): every PLX system and connection, and how far the
ontology covers each of them.

The registry, ``ontology/source_registry.json`` (format ``plx-source-registry/v1``), is
extracted from the upstream repositories at the repo-level pins of
``devloop/upstream-pins.json`` (``repo_pins``) with local git reads only. Nothing in it is
typed by hand: regenerating at the same pins reproduces it byte for byte. Entry kinds:

* ``portal`` ``api_route``: every Next.js route handler of the Portal repo (id = its
  directory, name = its URL), its HTTP ``methods``, the Prisma ``models`` it reads or
  writes and the reporting-API ``relations`` its SQL names, in the route file or in the
  first-party modules it imports (a module reached by more than half of all routes is
  the shared auth/audit layer and is not attributed); a table its SQL names that no model or
  relation resolves is kept in ``unresolved_relations`` (not a CTE, function, schema or
  ``pg_`` catalog, and only from a literal holding a statement). SQL a module sends through a
  client that sets its own ``search_path`` with a first schema other than ``public`` (the
  module or a module it imports, through re-export modules) resolves an unqualified table only
  from the pinned catalogs of the database that client connects to (``search_path_database``:
  ``name``, the database literal of the module setting the path; where an environment variable
  overrides that literal (``override``, names only), the literal is only its ``default`` and
  the client has a ``name`` only when a line of the runbook names that variable with the
  catalog database alone; ``evidence``, where the modules the route reaches name the database
  or its override and set the path; ``catalog``, the citation that the pinned catalogs describe
  that database: the reporting manifest's relations and the Prisma models with an
  ``@@schema``, both of the Portal database that ``CATALOG_DATABASE_PATH`` names on the line
  giving the address consumers connect to; a client naming no database, or another one, has
  none): when the first schema of the path holds a relation of that name, it resolves to that
  relation or model, with the catalog citation in ``search_path_resolved`` (table ->
  ``relation``, ``evidence``). A table that schema's catalog does not list never falls through
  to the next schema: the manifest is an allowlist (a subset of a schema's relations) and
  Prisma does not list the tables raw SQL creates, so no pinned catalog is complete for a
  schema, and a table it does not list may still be in it and shadow the later one. Such a
  table is kept in ``search_path_relations`` with the client's ``search_path``, never guessed
  to be the ``public`` relation of the same name. A route with no data of its
  own lists the data the shared layer reaches in ``models_via_shared`` and
  ``relations_via_shared``, and in ``data_clients`` the data-client modules (Prisma or ``pg``
  client, Graph, BC: ``_DATA_CLIENT``) it may read data through that no literal names, e.g.
  ``db[getDelegate(model)]``: those of its full closure, or, where the shared layer reaches
  data, those its own closure holds or imports from the shared layer itself (through
  re-export modules);
* ``portal`` ``prisma_model``: every model of ``portal/prisma/schema.prisma``, its table and
  its ``@@schema`` (``schema``, absent: the datasource's, taken to be ``public``);
* ``portal-reporting-api`` ``relation``: every relation of the Workbench reporting-API
  manifest (Portal ``public.app_*``, FileMaker ``public.*``, ``sage.*``, ``silver.*``);
* ``<server>`` ``mcp_tool``: every registered MCP tool (``.tool``/``.registerTool``); the
  system is the server name declared in the same directory (``plx-mc``, ``plx-brain``);
* ``business-central`` ``entity_set`` (company-scoped API paths in request literals),
  ``web_service_action`` (published codeunit actions) and ``api_page`` (AL API pages);
* ``microsoft-graph`` ``endpoint``: Graph request templates (mail, SharePoint, Teams,
  directory) with every value folded to ``{}``, and the HTTP ``methods`` upstream code
  requests each with (``request_methods``; ``unknown`` for a request no rule decides, none
  for a template only comments name); ``sharepoint`` ``site``: named sites;
* ``external`` ``host`` (https hosts named in product code; reserved example/test names
  dropped), ``env_group`` (variables of ``.env`` example files grouped by their first
  token: names only, a value is never read) and ``sdk`` (service SDK dependencies).

Each entry: ``id`` (``<system>.<kind>:<key>``), ``system``, ``kind``, ``name``, the kind's
attributes and ``evidence``: up to five ``<repo>:<path>@<commit12>[:<line>]`` citations
(``evidence_total`` when there are more). Product code only (``is_product``): tests,
fixtures, docs, examples, ``tmp``, ``artifacts``, built output, hidden directories and
``node_modules`` are not read.

Coverage is computed on every read, never stored, from this repository's code and the
latest live results: **declared** (in the registry) -> **bound** (a binding of a served
ontology type reads it; a Portal relation or BC entity set the generator maps to a
``not_served`` type with an upstream ask, ``not_served`` of ``generated/meta/coverage.json``
or ``generated/meta/bc_coverage.json``, stops at declared with reason ``not_served``) ->
**captured** (Workbench capture code holds it: the pinned reporting catalog for Portal relations, ``bc_read`` capture sets, the mail and SharePoint
capture stores, for sites the SharePoint capture scope's targets and the estate scope's
discover rule less its site exclusions) -> **queryable** (the projector
has a capture source for its family, so the ontology tools read it) ->
**fixture_queryable** (synthetic rows of every binding that reads it, generated from the
schema by ``ontology/generator/fixture.py``, project into a temporary store and come
back through ``plx.find`` and ``plx.object`` for a reader granted every audience and
marking: the path works end to end, on fixture data, never live) -> **evaluated** (a
passing live check aligned with its source names it in its result fields: a relation of its
data keys or, for a Graph template, a reader operation of ``integrations/live_source_grammar.py``
whose template folds to it (``graph_operations``) or resolves to the same Graph resource
(``aligned_operations``), never the relation the template is bound to; a check is aligned with the systems ``LIVE_SOURCES`` maps its source to and the families
the bindings declaring those systems' entries read, e.g. ``plx-mc`` tools under
``mission_control``; an MCP tool only when that check's run also lists the tool under
``tools_called``: a check that read its bound data through another tool, or directly, is
recorded as ``bound_data_read`` and credits nothing). A route's
stages are those of its data: each holds only when it holds for every model and relation.
An entry's ``status`` is the highest stage whose predecessors all hold, or a terminal
status (``TERMINAL``): ``out_of_scope`` (``ontology/source_scope.json`` names its entry id,
or its ``<system>.<kind>``, with the decided ``devloop/DECISIONS`` record behind it; the
entry id wins), ``not_a_data_source`` (a route that names no data and reaches neither data
nor a data client, not even through the shared layer) or ``auth_layer_only`` (a route whose
only data is the shared auth layer's, which stays tracked as its ``prisma_model`` entries:
no data client or unresolved table of its own; ``AUTH_LAYER_DECISION``, while decided). A
route with data and an unresolved table stops at declared with reason
``unresolved_relation``; one with a ``search_path_relations`` table stops there with reason ``search_path_unresolved``,
or ``not_resolvable`` when no pinned catalog describes the client's database (ahead of ``reaches_data_client``; behind ``unresolved_relation`` when it has data keys). An entry whose only data keys are action types, each on a served
target, is terminal ``bound_action``: an action is verified by read-back on each call
(its action type's ``verification``), never captured or evaluated as data
(``devloop/DECISIONS/2026-10-08-gen-mc-registry-mcp-coverage.md``); an entry with data
keys too goes through the data stages on those keys alone. A terminal entry is not a gap
and not in the coverage denominator: the entries of the kinds with an ontology schema (``SCHEMA_KINDS``: data a
source family binds); MCP tools, hosts, environment groups, SDKs and web-service actions
are inventory, not data, until a binding or action type declares them.

An entry's data keys (``data_keys``, the one path from an entry to its bindings) are those
its kind names by relation (``KIND_KEYS``: a reporting relation, a Prisma table, a route's
models and relations, a BC entity set or API page, a SharePoint site) plus, for every kind,
those of each ontology binding (its ``(source, relation)``) and action type (``("action",
<id>)``: bound, never captured) that lists the entry's id in its ``registry_entries``, and,
for a Graph template, the family and relation the Graph generator resolved it to
(``ontology/schema/generated/meta/graph_endpoints.json``, ``ontology/generator/graph.py``).
A template that generator classifies as not business data, or cannot resolve against the
pinned Graph v1.0 metadata, has no data key: it stays declared with reason
``not_business_data`` (and the ``decision`` record behind it; a template every upstream
request of which writes is a ``write_operation``) or ``graph_unresolved``; one its rule
would bind but no upstream request is found to GET stays declared with reason
``graph_method_unknown``. A
template bound to a relation no capture reads whose generator rule declares why
(``blocked``) stops at ``captured`` with reason ``blocked``, the reason and its ``decision``
record; a ``blocked`` relation a capture does read is refused (a stale blocker).

Commands (repository root; local git only, no network):

    python3 -m scripts.source_registry generate [--checkout REPO=PATH ...]
    python3 -m scripts.source_registry --check [--checkout REPO=PATH ...]   (or generate --check)
    python3 -m scripts.source_registry status [--live FILE ... | --no-live] [--json]
    python3 -m scripts.source_registry gaps [--live FILE ... | --no-live] [--out FILE]

Checkouts: ``--checkout``, env ``PLX_DRIFT_CHECKOUT_<REPO>``, else the pin file's
``repos.<REPO>.default_checkout`` (``ontology/upstream.checkout_for``); a
shallow checkout needs the pinned commit fetched (``git fetch --depth 1 origin <sha>``).
``--check`` (the gate) exits 1 when a repo-level pin moved without regeneration, when the
entries were edited by hand, or, where every checkout holds its pinned commit, when a
regeneration differs; a missing checkout skips only that last comparison. Live results
default to ``results/latest.json`` and ``results/daily/latest.json`` of
``origin/claude/devloop-live`` (the newer run of a check wins); ``--live`` reads files.

Gaps for ``devloop/gaps.py`` (the gap ledger's one coverage path): ``gaps(registry, live,
RepoFacts(root))`` in process, where ``root`` holds the ``FACT_INPUTS`` files read from Git at
the ledger's ref, or ``gaps --out FILE``; format ``plx-source-gaps/v1``:

* ``registry``: ``path``, ``entries_digest``, ``upstream`` (repo -> pinned commit);
* ``live``: the result documents read (``from``, ``run``, ``generated_at``), [] if none;
* ``counts``: ``entries``, ``by_status`` (stage -> n), ``by_system`` (system -> stage -> n),
  ``coverage`` (``kinds``, ``entries`` of those kinds, and ``by_family``: source family ->
  ``entries`` and, per stage, the entries that reached it);
* ``fixture``: the synthetic probe behind ``fixture_queryable``: ``rows_per_relation`` and
  ``by_family`` (binding source family -> ``bindings``, ``fixture_queryable`` and
  ``not_queryable``: binding -> reason);
* ``out_of_scope``: every scope of ``ontology/source_scope.json`` with its ``decision`` and
  the number of ``entries`` it holds (0: a stale scope);
* ``uncovered``: every entry whose status is neither ``evaluated`` nor terminal, sorted by
  system, kind and id: ``id``, ``system``, ``kind``, ``name``, ``status``, ``missing`` (the
  first stage that does not hold), ``reason`` (``REASONS``), ``blocking`` (up to five of the entry's
  ``<family>:<relation>`` data keys that fail that stage, routes only), a route's
  ``unresolved_relations``, ``search_path``, ``search_path_relations`` and
  ``search_path_database``, ``evidence`` (the
  first citation); an MCP tool whose bound data a check read without calling it carries
  ``bound_data_read`` (up to five ``check``, ``result`` and ``read``: through another tool,
  naming the tools its run called, or a direct read when it called none); a ``no_live_check`` Graph
  template carries ``waits_for``: ``operations_read`` (a reader operation requests it; no
  check has recorded reading it) or ``reader_operation`` (no Workbench reader operation
  requests it); a Graph template not bound carries ``not_business_data`` (the reason)
  and ``decision`` (its record), ``unresolved`` (where resolution stopped) or
  ``method_unknown`` (the methods found); a ``blocked``
  one carries ``blocked`` (the reason) and ``decision`` (the record it waits on);
* ``bound_without_entry``: ontology bindings no registry entry reads (``binding``,
  ``source``, ``relation``): Workbench-side coverage the upstream code does not name; with
  ``registry_entry`` when a binding or action type declares an id the registry lacks.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ontology.generator.tool_registration import tool_registration  # noqa: E402
from ontology.source_scope import (DECIDED, SCOPE_FORMAT, SCOPE_KEY, SCOPE_PATH,  # noqa: E402
                                   RegistryError, load_scope)
from ontology.upstream import checkout_for, is_product  # noqa: E402

FORMAT = "plx-source-registry/v1"
GAPS_FORMAT = "plx-source-gaps/v1"
REGISTRY_PATH = ROOT / "ontology" / "source_registry.json"
PINS_PATH = ROOT / "devloop" / "upstream-pins.json"
LIVE_REF = "origin/claude/devloop-live"
LIVE_FILES = ("results/latest.json", "results/daily/latest.json")
GRAPH_ENDPOINTS = "ontology/schema/generated/meta/graph_endpoints.json"
PORTAL_CATALOG_PATH = ROOT / "ontology" / "schema" / "generated" / "meta" / "portal_catalog.json"
STAGES = ("declared", "bound", "captured", "queryable", "fixture_queryable", "evaluated")
TERMINAL = ("out_of_scope", "not_a_data_source", "auth_layer_only", "bound_action")
# The decided record behind ``auth_layer_only``; while it is missing or not decided such a
# route stays declared with reason ``models_via_shared``.
AUTH_LAYER_DECISION = "devloop/DECISIONS/2026-10-07-portal-route-auth-only.md"
ACTION = "action"   # the data-key family of an action type that declares a registry entry
REASONS = {
    "no_source_family": "no ontology binding or action type declares it",
    "not_business_data": "the Graph generator classifies it as not business data (see its "
                         "decision record)",
    "graph_unresolved": "the Graph template does not resolve against the pinned Graph v1.0 "
                        "metadata (a value folded a path segment, or a member v1.0 lacks)",
    "graph_method_unknown": "no upstream request of the Graph template is found to be a GET "
                            "(methods), so it is not read from (method_unknown)",
    "models_via_shared": "the route reaches data only through the shared auth/audit layer",
    "reaches_data_client": "the route names no data but its own code reaches a data client "
                           "(Prisma, pg, Graph, BC): it may read data no literal names",
    "unresolved_relation": "its SQL reads a table no Prisma model or reporting relation "
                           "resolves (unresolved_relations)",
    "search_path_unresolved": "its SQL names a table without a schema through a client that "
                              "sets its own search_path (search_path_relations): the pinned "
                              "catalogs do not show the first schema of that path to hold it",
    "not_resolvable": "its SQL names a table without a schema through a client that sets its "
                      "own search_path on a database no pinned catalog describes "
                      "(search_path_database): which relation it reads is not resolvable from "
                      "pinned evidence until a catalog of that database is pinned",
    "no_binding": "no ontology binding reads it",
    "not_served": "only a not_served type with an upstream ask declares it",
    "not_in_catalog": "not a relation of the pinned reporting catalog",
    "not_captured": "no Workbench capture code holds it",
    "blocked": "no Workbench capture holds it, for the declared reason (see its decision "
               "record)",
    "out_of_scope": "not in the Workbench capture scope",
    "not_projected": "the projector has no capture source for its family",
    "fixture_not_queryable": "synthetic rows of a binding reading it did not come back through "
                             "plx.find/plx.object",
    "no_live_results": "no live results were read",
    "no_live_check": "no live check aligned with its source names it",
    "bound_data_read": "a live check read its bound data without calling the tool (through "
                       "another tool or a direct read, see bound_data_read): no run's "
                       "tools_called lists the tool, so it is not credited",
    "live_not_passing": "the live checks that name it did not pass",
    "live_unexercised": "the live checks that name it read it empty: bound, but nothing was "
                        "projected or read back",
}
# A reason split out of earlier ones -> those reasons: its entries were reported under them
# before, so the gap ledger keeps their gap ids as aliases (``former``) of its re-keyed gap.
REASON_FORMER = {"not_resolvable": ("search_path_unresolved",)}
SPECS_DIR = ROOT / "devloop" / "live-checks"
UNEXERCISED = "unexercised"   # a name a check's registry_names.unexercised fields carry
MAX_EVIDENCE = 5
# Entry kinds with an ontology schema (the coverage denominator) -> the source family an
# entry of the kind counts under when its data keys name none (an endpoint no capture
# family reads stays a Graph entry).
SCHEMA_KINDS = {"relation": "portal_api", "prisma_model": "portal_api", "api_route": "portal_api",
                "entity_set": "bc", "api_page": "bc", "site": "sharepoint",
                "endpoint": "microsoft-graph"}

PORTAL = "plx-customer-portal"
PRISMA_PATH = "portal/prisma/schema.prisma"
MANIFEST_PATH = "services/workbench-api/manifest/allowlist.json"
# The runbook naming the database the reporting manifest's replica holds: the Portal database,
# a physical copy of the one the Prisma schema migrates, so both catalogs describe it.
CATALOG_DATABASE_PATH = "docs/runbooks/WORKBENCH-READONLY-DB-ACCESS.md"
# The database is read from its line giving the address consumers connect to (`<host>`, port
# <n>, database `<name>`), never from other prose naming a database (e.g. an admin URL's).
_CATALOG_DATABASE = re.compile(r"^\s*`[^`\s]+`,\s*port\s+\d+,\s*database\s+`([a-z_][a-z0-9_]*)`",
                               re.M)
_RUNBOOK_DATABASE = re.compile(r"\bdatabase `([a-z_][a-z0-9_]*)`|"
                               r"""postgres(?:ql)?://[^\s"'`/]*/([a-z_][a-z0-9_]*)""")
_RUNBOOK_VARIABLE = re.compile(r"\b[A-Z][A-Z0-9_]*[A-Z0-9]\b")
# The database a pg client module connects to: a database-name constant or a URL path.
_CLIENT_DATABASE = re.compile(r"""\b[A-Z_]*DATABASE[A-Z_]*\s*=\s*["'`]([a-z_][a-z0-9_]*)["'`]|"""
                              r"""postgres(?:ql)?://[^\s"'`/]*/([a-z_][a-z0-9_]*)""")
# An environment variable overriding that database (its name or a connection URL): the
# client's database literal is then only a default.
_ENV_DATABASE = re.compile(r"""\b(?:process\.env(?:\.|\[\s*["'`])|os\.environ(?:\.get\(|\[)\s*["'])"""
                           r"""((?:[A-Z0-9]+_)*(?:DATABASE|PGDATABASE|DB|PG|POSTGRES(?:QL)?)"""
                           r"""(?:_(?:NAME|URL|URI))?)\b""")
CODE = re.compile(r"\.(?:ts|tsx|js|jsx|mjs|cjs|py|al)$")
LITERAL = re.compile(r"`(?:[^`\\]|\\.)*`|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'")

# Next.js app router: a route handler file and its exported HTTP methods.
ROUTE = re.compile(r"^(?P<app>(?:.+/)?src/app)/(?:(?P<path>.+)/)?route\.(?:ts|tsx|js|mjs)$")
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_EXPORTED = re.compile(r"\bexport\s+(?:async\s+)?(?:function\s*\*?\s*|const\s+|let\s+|var\s+)"
                       r"(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)\b")
_EXPORT_LIST = re.compile(r"\bexport\s*\{([^}]*)\}")
_IMPORT = re.compile(r"""\b(?:from|import|require)\s*\(?\s*["']([^"']+)["']""")
_RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts",
                     "/index.tsx", "/index.js", "/index.mjs")
_TS_PATHS = re.compile(r'"([^"*]+)\*"\s*:\s*\[\s*"([^"*]+)\*"')
_TS_BASE = re.compile(r'"baseUrl"\s*:\s*"([^"]+)"')

# Prisma: model blocks, their table, and client operations on a model accessor.
_MODEL = re.compile(r"^model\s+(\w+)\s*\{")
_MAP = re.compile(r'^\s*@@map\(\s*"([^"]+)"')
_PRISMA_SCHEMA = re.compile(r'^\s*@@schema\(\s*"([^"]+)"')
_PRISMA_OPS = ("findUnique|findUniqueOrThrow|findFirst|findFirstOrThrow|findMany|create|"
               "createMany|createManyAndReturn|update|updateMany|updateManyAndReturn|upsert|"
               "delete|deleteMany|count|aggregate|groupBy")
_MODEL_REF = re.compile(r"\.\s*([a-z][A-Za-z0-9]*)\s*\.\s*(?:%s)\s*[<(]" % _PRISMA_OPS)
# SQL in string literals (``$queryRaw``, ``fmQuery``): upper-case keywords, as the code writes them.
_SQL_REF = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+\\?"?([a-z_][a-z0-9_]*)\\?"?'
                      r'(?:\s*\.\s*\\?"?([a-z_][a-z0-9_]*)\\?"?)?')
# A table name no model or relation resolves is kept (``unresolved_relations``) only from a
# literal holding a statement, never one a statement defines or calls rather than reads: a CTE
# (``WITH name AS (``, ``, name AS (``, a fragment's ``name AS (`` line), a function
# (``FROM unnest(``), a schema, a ``pg_`` system catalog.
_SQL_STATEMENT = re.compile(r"\b(?:SELECT|INSERT\s+INTO|DELETE\s+FROM)\b|\bUPDATE\s+\S+\s+SET\b")
_SQL_CTE = re.compile(r"(?:\bWITH(?:\s+RECURSIVE)?|,|^)\s*([a-z_][a-z0-9_]*)\s+AS\s+"
                      r"(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.M)
_SQL_CALL = re.compile(r"\s*\(")
_PRISMA_RAW = re.compile(r"(?:\bPrisma\.sql|\$(?:query|execute)Raw(?:Unsafe)?(?:<[^>]*>)?\s*\(?)\s*$")
# A client that sets its session's schema search path (``SET search_path TO v2, public``).
_SEARCH_PATH = re.compile(r"\bSET\s+search_path\s+(?:TO|=)\s*([a-z_][a-z0-9_]*(?:\s*,\s*[a-z_][a-z0-9_]*)*)",
                          re.I)
# A module made only of re-exports (``export * from``): importing it imports what it re-exports.
_REEXPORTS = re.compile(r"""\A(?:\s|//[^\n]*|/\*.*?\*/|export\s+(?:type\s+)?(?:\*(?:\s+as\s+\w+)?|"""
                        r"""\{[^}]*\})\s+from\s+["'][^"']+["'];?)*\Z""", re.S)

_TOOL = tool_registration()
_SERVER = re.compile(r"""new\s+(?:Mcp)?Server\(\s*\{?[^)]{0,200}?\bname\s*:\s*["'`]([^"'`]+)["'`]""",
                     re.S)

_BC_FILE = re.compile(r"businesscentral|business-central|business_central|bcRequest|bcBatch|"
                      r"listCompanyEntity")
# A BC API path: an entity set after the company segment (``companies(<id>)``, or a template
# value naming the company/segment) or after a keyed entity (``salesOrders(<id>)/<lines>``).
_BC_PATHS = (
    re.compile(r"(?:companies\([^)\"'`]*\)|\$?\{[^}]*(?i:seg|compan)[^}]*\})/([a-z][A-Za-z0-9]+)"),
    re.compile(r"\b[a-z][A-Za-z0-9]*\([^)\"'`]*\)/([a-z][A-Za-z0-9]+)"),
)
_BC_CALL = re.compile(r"""\b(?:bcRequest|listCompanyEntity)(?:<.*?>)?\(\s*(?:[^,()"'`]+,\s*)?"""
                      r"""["'`]([a-z][A-Za-z0-9]+)["'`]""")
_BC_ACTION = re.compile(r"""bcWebServiceAction(?:<.*?>)?\(\s*"""
                        r"""["'`]([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+)["'`]""")
_AL_PAGE = re.compile(r"EntitySetName\s*=\s*'([A-Za-z0-9]+)'")

# Microsoft Graph v1.0 root resources: a request literal starting with one is a Graph call.
GRAPH_ROOTS = frozenset({
    "applications", "auditLogs", "chats", "communications", "devices",
    "directoryObjects", "domains", "drives", "groups", "identity", "invitations", "me",
    "organization", "places", "planner", "policies", "reports", "security",
    "servicePrincipals", "shares", "sites", "solutions", "subscriptions", "teams", "users"})
_GRAPH_LITERAL = re.compile(r"^(?:https://graph\.microsoft\.com/(?:v1\.0|beta))?(?:\$?\{[^{}]*\})?"
                            r"(/[A-Za-z][^?#\n]*)")
_GRAPH_KEYWORD = re.compile(r"^(\$?[a-z][A-Za-z0-9]*|microsoft\.graph\.[A-Za-z.]+)(?:\(.*)?$")
_GRAPH_FILE = re.compile(r"graph", re.I)
_SP_SITE = re.compile(r"[a-z0-9-]+\.sharepoint\.com:?/sites/([A-Za-z0-9_-]+)")
_HOST = re.compile(r"""["'`]https://([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"""
                   r"""(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)(?=[/"'`:?#])""")
# RFC 2606 / 6761 reserved names: documentation and test hosts, never a real integration.
_RESERVED_HOST = re.compile(r"(?:^|\.)(?:example|test|invalid|localhost|local)$|"
                            r"(?:^|\.)example\.(?:com|net|org)$")
_ENV_EXAMPLE = re.compile(r"(?:^|/)(?:\.env[^/]*\.example[^/]*|[^/]*\.env\.example)$")
_ENV_NAME = re.compile(r"^[ \t]*(?:export[ \t]+)?#?[ \t]*([A-Z][A-Z0-9_]*)[ \t]*=", re.M)
# A module that holds a data client: its importers can read data without naming it.
_DATA_CLIENT = re.compile(r"""@prisma/client|\bnew\s+PrismaClient\b|\bfrom\s+["']pg["']|"""
                          r"""\brequire\(\s*["']pg["']\s*\)|graph\.microsoft\.com|""" + _BC_FILE.pattern)
_SDK = re.compile(r"(?:^|[@/-])(?:sdk|client|api)(?:$|[/-])")

# Live-check ``source`` tokens (``devloop/live-checks/*.json``) -> the registry systems and
# binding source families whose names their results can carry; ``LiveIndex`` adds the
# families the bindings declaring those systems' entries read (``RepoFacts.system_families``).
LIVE_SOURCES = {
    "business-central": {"business-central", "bc"},
    "portal-workbench-api": {"portal", "portal-reporting-api", "portal_api"},
    "microsoft-graph": {"microsoft-graph", "sharepoint", "graph_mail", "teams"},
    "sharepoint-graph": {"microsoft-graph", "sharepoint"},
    "sharepoint": {"sharepoint"},
    "mission-control-mcp": {"plx-mc"},
    "vmc": {"plx-brain"},
}


# --------------------------------------------------------------------------- upstream reads

class Tree:
    """One upstream repository at one pinned commit, read with local git only."""

    def __init__(self, repo: str, checkout: Path, commit: str) -> None:
        self.repo, self.checkout, self.commit = repo, Path(checkout), commit
        listing = self._git("ls-tree", "-r", "-z", "--name-only", commit)
        self.paths = [p for p in listing.decode("utf-8", "replace").split("\0") if p]
        self._text: dict[str, str] = {}

    @staticmethod
    def holds(checkout: Path, commit: str) -> bool:
        done = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", f"{commit}^{{commit}}"],
                              capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return done.returncode == 0

    def _git(self, *args: str, stdin: bytes | None = None) -> bytes:
        done = subprocess.run(["git", "-C", str(self.checkout), *args], input=stdin,
                              capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if done.returncode:
            raise RegistryError(f"git {args[0]} failed in {self.checkout}: "
                                f"{done.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return done.stdout

    def read(self, paths: Iterable[str]) -> dict[str, str]:
        paths = list(paths)
        wanted = [p for p in paths if p not in self._text]
        if wanted:
            out = self._git("cat-file", "--batch",
                            stdin="".join(f"{self.commit}:{p}\n" for p in wanted).encode())
            pos = 0
            for path in wanted:
                end = out.index(b"\n", pos)
                header = out[pos:end].split()
                if header[-1] == b"missing":
                    pos = end + 1
                    continue
                size = int(header[2])
                self._text[path] = out[end + 1:end + 1 + size].decode("utf-8", "replace")
                pos = end + 2 + size
        return {p: self._text[p] for p in paths if p in self._text}

    def code(self) -> dict[str, str]:
        """Every product source file (``is_product``) with its text."""
        return self.read(p for p in self.paths if CODE.search(p) and is_product(p))

    def ref(self, path: str, line: int | None = None) -> str:
        return f"{self.repo}:{path}@{self.commit[:12]}" + (f":{line}" if line else "")


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _literals(text: str) -> Iterable[tuple[int, str]]:
    for m in LITERAL.finditer(text):
        yield m.start(), m.group()[1:-1]


class Inventory:
    """Entries keyed by id; repeated sightings merge their evidence and list attributes."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.refs: dict[str, set[str]] = defaultdict(set)

    def add(self, system: str, kind: str, key: str, ref: str, *, name: str | None = None,
            **attrs: Any) -> None:
        entry_id = f"{system}.{kind}:{key}"
        entry = self.entries.setdefault(entry_id, {"id": entry_id, "system": system, "kind": kind,
                                                   "name": key if name is None else name})
        for attr, value in attrs.items():
            if value is None:
                continue
            if isinstance(value, (list, set, tuple)):
                entry[attr] = sorted(set(entry.get(attr, ())) | set(value))
            else:
                entry.setdefault(attr, value)
        self.refs[entry_id].add(ref)

    def finish(self) -> list[dict[str, Any]]:
        out = []
        for entry_id in sorted(self.entries):
            entry = dict(self.entries[entry_id])
            refs = sorted(self.refs[entry_id])
            entry["evidence"] = refs[:MAX_EVIDENCE]
            if len(refs) > MAX_EVIDENCE:
                entry["evidence_total"] = len(refs)
            out.append(entry)
        return out


# --------------------------------------------------------------------------- extractors

def prisma_models(text: str) -> dict[str, tuple[str, int, str | None]]:
    """Model name -> (table, line, ``@@schema`` or None) of a Prisma schema."""
    models: dict[str, tuple[str, int, str | None]] = {}
    current: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if current is None:
            m = _MODEL.match(line)
            if m:
                current = m.group(1)
                models[current] = (current, number, None)
        elif line.startswith("}"):
            current = None
        elif (m := _MAP.match(line)):
            models[current] = (m.group(1),) + models[current][1:]
        elif (m := _PRISMA_SCHEMA.match(line)):
            models[current] = models[current][:2] + (m.group(1),)
    return models


def _route_url(path: str | None) -> str:
    segments = [s for s in (path or "").split("/")
                if s and not (s.startswith("(") and s.endswith(")")) and not s.startswith("@")]
    return "/" + "/".join(segments)


def _methods(text: str) -> list[str]:
    found = set(_EXPORTED.findall(text))
    for group in _EXPORT_LIST.findall(text):
        for item in group.split(","):
            name = item.split(" as ")[-1].strip()
            if name in HTTP_METHODS:
                found.add(name)
    return [m for m in HTTP_METHODS if m in found]


class _Resolver:
    """First-party import resolution with each file's nearest ``tsconfig.json`` aliases."""

    def __init__(self, tree: Tree, files: Mapping[str, str]) -> None:
        self.files = files
        self.aliases: dict[str, list[tuple[str, str]]] = {}
        for path, text in tree.read(p for p in tree.paths
                                    if posixpath.basename(p) == "tsconfig.json").items():
            base = posixpath.dirname(path)
            m = _TS_BASE.search(text)
            if m:
                base = posixpath.normpath(posixpath.join(base, m.group(1)))
            self.aliases[posixpath.dirname(path)] = [
                (alias, posixpath.normpath(posixpath.join(base, target)))
                for alias, target in _TS_PATHS.findall(text)]

    def _alias_of(self, path: str) -> list[tuple[str, str]]:
        directory = posixpath.dirname(path)
        while True:
            if directory in self.aliases:
                return self.aliases[directory]
            if not directory:
                return []
            directory = posixpath.dirname(directory)

    def resolve(self, importer: str, spec: str) -> str | None:
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
        else:
            base = next((target + "/" + spec[len(alias):]
                         for alias, target in self._alias_of(importer) if spec.startswith(alias)), None)
            if base is None:
                return None
            base = posixpath.normpath(base)
        return next((base + s for s in _RESOLVE_SUFFIXES if base + s in self.files), None)


def _manifest_lines(text: str, rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """The line of each manifest entry's ``relation`` (the entries in file order)."""
    pos, lines = text.find('"entries"'), []
    for row in rows:
        m = None if pos < 0 else re.compile(
            r'"relation"\s*:\s*%s' % re.escape(json.dumps(row["relation"]))).search(text, pos)
        if m is None:
            raise RegistryError(f"{MANIFEST_PATH}: entry {row['relation']!r} not found in order")
        lines.append(_line(text, m.start()))
        pos = m.end()
    return lines


def portal_entries(inv: Inventory, tree: Tree, code: Mapping[str, str]) -> None:
    schema = tree.read([PRISMA_PATH]).get(PRISMA_PATH)
    manifest = tree.read([MANIFEST_PATH]).get(MANIFEST_PATH)
    if schema is None or manifest is None:
        raise RegistryError(f"{tree.repo}@{tree.commit[:12]} lacks {PRISMA_PATH} or "
                            f"{MANIFEST_PATH}")
    models = prisma_models(schema)
    table_model = {(db_schema or "public", table): model
                   for model, (table, _, db_schema) in models.items()}
    # The pinned catalogs of the Portal database: table -> schema -> citation, from the Prisma
    # models that state their schema and the reporting manifest's relations. Only a client
    # of that database (``catalog_database``) resolves through them.
    catalog: dict[str, dict[str, str]] = defaultdict(dict)
    for model, (table, line, db_schema) in models.items():
        inv.add("portal", "prisma_model", model, tree.ref(PRISMA_PATH, line), table=table,
                schema=db_schema)
        if db_schema:
            catalog[table][db_schema] = tree.ref(PRISMA_PATH, line)
    relations = set()
    rows = json.loads(manifest).get("entries", [])
    for row, line in zip(rows, _manifest_lines(manifest, rows)):
        name = f"{row['schema']}.{row['relation']}"
        relations.add(name)
        catalog[row["relation"]].setdefault(row["schema"], tree.ref(MANIFEST_PATH, line))
        inv.add("portal-reporting-api", "relation", name, tree.ref(MANIFEST_PATH, line),
                schema=row["schema"], domain=row.get("domain"),
                prisma_model=table_model.get((row["schema"], row["relation"])))
    schemas = {s for held in catalog.values() for s in held}
    runbook = tree.read([CATALOG_DATABASE_PATH]).get(CATALOG_DATABASE_PATH) or ""
    named_dbs = {m.group(1): m.start() for m in _CATALOG_DATABASE.finditer(runbook)}
    catalog_database = next((name, tree.ref(CATALOG_DATABASE_PATH, _line(runbook, at)))
                            for name, at in named_dbs.items()) if len(named_dbs) == 1 else None
    # Environment variable -> (databases the runbook lines naming it name, their citations).
    runbook_vars: dict[str, tuple[set[str], list[str]]] = defaultdict(lambda: (set(), []))
    for number, line in enumerate(runbook.splitlines(), 1):
        dbs = {m.group(1) or m.group(2) for m in _RUNBOOK_DATABASE.finditer(line)}
        for var in set(_RUNBOOK_VARIABLE.findall(line)) if dbs else ():
            runbook_vars[var][0].update(dbs)
            runbook_vars[var][1].append(tree.ref(CATALOG_DATABASE_PATH, number))

    def on_path(table: str, path: tuple[str, ...]) -> tuple[str, str] | None:
        """(schema, citation) of ``table`` when the catalogs list it in the first schema of
        ``path``; else None (no catalog is complete for a schema: see the module docstring)."""
        cite = catalog.get(table, {}).get(path[0])
        return (path[0], cite) if cite else None
    accessors = {model[0].lower() + model[1:]: model for model in models}
    # SQL composes CTEs across literals and modules: a name any of them defines is no table.
    ctes = {name for text in code.values() for name in _SQL_CTE.findall(text)}

    def data(text: str, client: tuple[tuple[str, ...], str | None] | None) -> tuple[set, ...]:
        search_path = client[0] if client else None
        cataloged = bool(client and catalog_database and client[1] == catalog_database[0])
        used = {accessors[a] for a in _MODEL_REF.findall(text) if a in accessors}
        named: set[str] = set()
        unresolved: set[str] = set()
        pathed: set[str] = set()
        resolved: set[tuple[str, str, str]] = set()   # (table, schema.table, citation)
        for pos, literal in _literals(text):
            statement = _SQL_STATEMENT.search(literal)
            # Prisma raw SQL runs on the Prisma datasource, never through the pg client.
            pathed_literal = bool(search_path) and not _PRISMA_RAW.search(
                text, max(0, pos - 80), pos)
            for ref in _SQL_REF.finditer(literal):
                first, second = ref.groups()
                schema_name, table = (first, second) if second else ("public", first)
                if second and schema_name not in schemas:
                    continue
                if pathed_literal and not second:
                    if statement and not (table in ctes or table in schemas or table.startswith(
                            "pg_") or _SQL_CALL.match(literal, ref.end())):
                        held = on_path(table, search_path) if cataloged else None
                        if held is None:
                            pathed.add(table)
                        elif (held[0], table) in table_model:
                            used.add(table_model[(held[0], table)])
                        else:
                            named.add(f"{held[0]}.{table}")
                        if held is not None:
                            resolved.add((table, f"{held[0]}.{table}", held[1]))
                elif (schema_name, table) in table_model:
                    used.add(table_model[(schema_name, table)])
                elif second or f"public.{table}" in relations:
                    named.add(f"{schema_name}.{table}")
                elif statement and not (table in ctes or table in schemas or table.startswith(
                        "pg_") or _SQL_CALL.match(literal, ref.end())):
                    unresolved.add(table)
        return used, named, unresolved, pathed, resolved

    resolver = _Resolver(tree, code)
    deps = {path: {r for spec in _IMPORT.findall(text) if (r := resolver.resolve(path, spec))}
            for path, text in code.items()}
    # module -> (search path, database it connects to, default database, override variables)
    # -> citations of the SET, the database or default and the overrides; a module naming two
    # databases names none it is known to connect to, and one whose database an environment
    # variable overrides connects to the catalog database only where a runbook line names
    # that variable with that database alone (else to none known).
    setters: dict[str, dict[tuple, list[str]]] = defaultdict(dict)
    for path, text in code.items():   # in a string, not a comment
        databases = {m.group(1) or m.group(2): m.start() for m in _CLIENT_DATABASE.finditer(text)}
        literal = next(iter(databases)) if len(databases) == 1 else None
        overrides = {m.group(1): m.start() for m in _ENV_DATABASE.finditer(text)}
        paired = [v for v in overrides
                  if catalog_database and runbook_vars[v][0] == {catalog_database[0]}]
        database = (catalog_database[0] if paired else None) if overrides else literal
        default = literal if overrides else None
        spots = set(overrides.values()) | ({databases[literal]} if literal else set())
        for pos, string in _literals(text):
            for m in _SEARCH_PATH.finditer(string):
                key = (tuple(s.strip().lower() for s in m.group(1).split(",")), database,
                       default, tuple(sorted(overrides)))
                setters[path].setdefault(key, [tree.ref(path, n) for n in sorted(
                    {_line(text, at) for at in {pos} | spots})] + sorted(
                    {c for v in paired for c in runbook_vars[v][1]}))

    def search_path_of(module: str) -> tuple[tuple | None, frozenset[str]]:
        """The non-default search path, and database, of the client ``module`` sets or imports
        (through re-export modules), and the modules setting it; two different ones are
        refused, never one picked."""
        held: dict[tuple, set[str]] = defaultdict(set)
        seen, stack = set(), [module]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for key in setters.get(current, ()):
                if key[0][0] != "public":
                    held[key].add(current)
            if current == module or _REEXPORTS.match(code[current]):
                stack.extend(deps.get(current, ()))
        if len(held) > 1:
            raise RegistryError(f"{tree.repo}@{tree.commit[:12]}:{module} reaches clients with "
                                f"different search paths: {sorted(held, key=str)}")
        return next(((key, frozenset(by)) for key, by in held.items()), (None, frozenset()))

    def client_evidence(client: tuple, modules: Iterable[str]) -> dict[str, Any]:
        """``search_path_database``: the client's database (or default and overrides), where
        the ``modules`` setting it that the route reaches name it and set its path, and the
        catalog citation when the pinned catalogs describe that database."""
        out: dict[str, Any] = {"evidence": [c for m in sorted(modules) for c in setters[m][client]]}
        for field_name, value in zip(("name", "default", "override"), client[1:]):
            if value:
                out[field_name] = list(value) if isinstance(value, tuple) else value
        if catalog_database and client[1] == catalog_database[0]:
            out["catalog"] = catalog_database[1]
        return out

    module_client = {path: search_path_of(path) for path in code}
    module_path = {path: client for path, (client, _) in module_client.items()}
    found = {path: data(text, module_path[path]) for path, text in code.items()}
    routes = {path: m for path in code if (m := ROUTE.match(path))}

    def closure(start: str, skip: frozenset = frozenset()) -> set[str]:
        seen, stack = {start}, [start]
        while stack:
            for nxt in deps.get(stack.pop(), ()):
                if nxt not in seen and nxt not in skip:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    reach: dict[str, int] = defaultdict(int)
    for route in routes:
        for module in closure(route) - {route}:
            reach[module] += 1
    shared = frozenset(m for m, n in reach.items() if n * 2 > len(routes))

    def reached(modules: Iterable[str]) -> tuple[set[str], ...]:
        out: tuple[set, ...] = (set(), set(), set(), set(), set())
        for module in modules:
            for held, more in zip(out, found[module]):
                held |= more
        return out

    def imported_shared(own: set[str]) -> set[str]:
        """The shared modules ``own`` imports itself (through re-export modules): a data
        client among them, e.g. the Prisma or pg client, is one the route may query."""
        out, stack = set(), [d for module in own for d in deps.get(module, ()) if d in shared]
        while stack:
            module = stack.pop()
            if module not in out:
                out.add(module)
                if _REEXPORTS.match(code[module]):
                    stack.extend(d for d in deps.get(module, ()) if d in shared)
        return out

    for route, m in routes.items():
        own = closure(route, shared)
        used, named, unresolved, pathed, resolved = reached(own)
        paths = {module_path[m] for m in own if found[m][3] or found[m][4]}
        if len(paths) > 1:
            raise RegistryError(f"{tree.repo}@{tree.commit[:12]}:{route} reads unqualified "
                                f"tables under different search paths: {sorted(paths, key=str)}")
        client = next(iter(paths)) if paths else None
        setter_modules = {s for m in own if found[m][3] or found[m][4] for s in module_client[m][1]}
        via_used, via_named, clients = (), (), ()
        if not used and not named:
            full = closure(route)
            via_used, via_named, *_ = reached(full - own)
            # Past a shared layer that reaches data, only the clients the route reaches itself.
            held = own | imported_shared(own) if via_used or via_named else full
            clients = [p for p in held if _DATA_CLIENT.search(code[p])]
        inv.add("portal", "api_route", posixpath.dirname(route), tree.ref(route),
                name=_route_url(m.group("path")), methods=_methods(code[route]), models=used,
                relations=named, unresolved_relations=sorted(unresolved) or None,
                search_path=", ".join(client[0]) if client else None,
                search_path_database=client_evidence(client, setter_modules) if client else None,
                search_path_relations=sorted(pathed) or None,
                search_path_resolved={t: {"relation": r, "evidence": e}
                                      for t, r, e in sorted(resolved)} or None,
                models_via_shared=sorted(via_used) or None,
                relations_via_shared=sorted(via_named) or None,
                data_clients=sorted(clients) or None)


def _server_names(code: Mapping[str, str]) -> dict[str, str]:
    by_dir = {}
    for path, text in sorted(code.items()):
        m = _SERVER.search(text)
        if m:
            by_dir.setdefault(posixpath.dirname(path), m.group(1))
    return by_dir


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def mcp_entries(inv: Inventory, tree: Tree, code: Mapping[str, str]) -> None:
    servers = _server_names(code)
    for path, text in code.items():
        for m in _TOOL.finditer(text):
            directory = posixpath.dirname(path)
            while directory not in servers and directory:
                directory = posixpath.dirname(directory)
            server = servers.get(directory, f"mcp-{tree.repo}")
            inv.add(_slug(server), "mcp_tool", m.group(1), tree.ref(path, _line(text, m.start())),
                    server=server)


def bc_entries(inv: Inventory, tree: Tree, code: Mapping[str, str]) -> None:
    for path, text in code.items():
        if path.endswith(".al"):
            for m in _AL_PAGE.finditer(text):
                inv.add("business-central", "api_page", m.group(1),
                        tree.ref(path, _line(text, m.start())))
            continue
        if not _BC_FILE.search(text):
            continue
        for start, literal in _literals(text):
            for name in {n for pattern in _BC_PATHS for n in pattern.findall(literal)}:
                inv.add("business-central", "entity_set", name, tree.ref(path, _line(text, start)))
        for m in _BC_CALL.finditer(text):
            inv.add("business-central", "entity_set", m.group(1),
                    tree.ref(path, _line(text, m.start())))
        for m in _BC_ACTION.finditer(text):
            inv.add("business-central", "web_service_action", m.group(1),
                    tree.ref(path, _line(text, m.start())))


def graph_template(path: str) -> tuple[str, list[str]]:
    """A Graph request path as a template (values -> ``{}``) and the literal site names in it."""
    path = re.sub(r"\$?\{[^{}]*\}", "{}", path)
    path = re.sub(r"\$?\{(?!\})[^}]*$", "{}", path)   # an expression the literal cut off
    out: list[str] = []
    sites: list[str] = []
    drive_path = False
    for raw in path.strip("/").split("/"):
        if not raw:
            continue
        segment, colon = (raw[:-1], ":") if raw.endswith(":") else (raw, "")
        if drive_path:
            if colon:
                out[-1] = "{}:"
                drive_path = False
            continue
        m = _GRAPH_KEYWORD.match(segment)
        if m and segment != "{}":
            out.append(m.group(1) + colon)
            drive_path = segment == "root" and bool(colon)
            if drive_path:
                out.append("{}")
        else:
            if out and out[-1] == "sites" and segment and "{" not in segment and "." not in segment:
                sites.append(segment)
            out.append("{}" + colon)
    return "/" + "/".join(out), sites


# The HTTP method of a Graph request literal, read from the call that sends it: a
# ``method`` option or a method string argument of that call, the verb of a Graph SDK chain
# (``.api(url).get()``) or of the call itself (``requests.post``, ``client.delete``), ``fetch``
# without a method (GET), or the same rules inside a wrapper defined in the same file. A
# literal assigned to a variable is followed to the calls that use it; one inside a call that
# only builds a URL (``new URL``, a helper without a request) to the call around that.
# Anything else (a body value, a returned path, a wrapper in another file) is
# ``UNKNOWN_METHOD``: never guessed to be a read. A literal in a comment is no request.
UNKNOWN_METHOD = "unknown"
_SDK_VERBS = {"get": "GET", "getstream": "GET", "post": "POST", "put": "PUT",
              "putstream": "PUT", "patch": "PATCH", "update": "PATCH", "delete": "DELETE",
              "del": "DELETE"}
_CALLEE = re.compile(r"([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*$")
_CHAINED = re.compile(r"\s*\.\s*([A-Za-z_$][\w$]*)\s*\(")
_METHOD_OPTION = re.compile(r"""\bmethod\s*[:=]\s*["'`](GET|POST|PUT|PATCH|DELETE)["'`]""",
                            re.I)
_METHOD_ARGUMENT = re.compile(r"""^\s*["'](GET|POST|PUT|PATCH|DELETE)["']\s*$""", re.I)
_VERB_CALLEE = re.compile(r"(?:^|[._])(get|post|put|patch|delete|download)$"
                          r"|^(?:graph|_graph|http|request)?(get|post|put|patch|delete|download)$",
                          re.I)
_URL_BUILDERS = frozenset({"URL", "encodeURI", "String", "str"})
_ASSIGNED = re.compile(r"(?:^|[^\w$.])([A-Za-z_$][\w$]*)\s*(?::[^=;\n]*)?=(?![=>])"
                       r"(?:[^=;]|==+|[!<>]=+)*$")
_DEFAULT_METHOD = re.compile(r"""\bmethod\s*(?::\s*\w+\s*)?=\s*["'](GET|POST|PUT|PATCH|DELETE)["']""",
                             re.I)
_REQUEST_CALL = re.compile(r"(?<![\w$])(?:fetch|[\w$]+\.(?:get|post|put|patch|delete|request)"
                           r"|requests\.\w+)\s*\(")
_METHOD_DEPTH = 4


def _blank_literals(text: str) -> str:
    """``text`` with every string literal's content blanked (same length), so brackets
    inside strings do not count."""
    out = list(text)
    for m in LITERAL.finditer(text):
        out[m.start() + 1:m.end() - 1] = " " * (m.end() - m.start() - 2)
    return "".join(out)


def _close(blank: str, opened: int) -> int | None:
    depth = 0
    for j in range(opened, len(blank)):
        if blank[j] in "([{":
            depth += 1
        elif blank[j] in ")]}":
            depth -= 1
            if depth == 0:
                return j
    return None


def _without_type_arguments(head: str) -> str:
    """``head`` (the text before a call's parenthesis) without its type arguments
    (``graphFetch<{ value: T[] }>``), which may nest."""
    if not head.rstrip().endswith(">"):
        return head
    level, i = 0, len(head.rstrip()) - 1
    while i >= 0:
        level += head[i] == ">"
        level -= head[i] == "<"
        if level == 0:
            return head[:i]
        i -= 1
    return head


def _arguments(text: str, blank: str) -> list[str]:
    """A call's argument texts, split at the top-level commas of ``blank`` (its text with
    string contents blanked)."""
    parts, depth, begin = [], 0, 0
    for i, c in enumerate(blank):
        depth += c in "([{"
        depth -= c in ")]}"
        if c == "," and depth == 0:
            parts.append(text[begin:i])
            begin = i + 1
    return parts + [text[begin:]]


def request_methods(text: str, blank: str, start: int, depth: int = 0) -> set[str]:
    """The HTTP methods the expression at ``start`` (a literal, or a name holding one) is
    requested with (rules above); empty when no rule decides."""
    if depth > _METHOD_DEPTH:
        return set()
    level, i, statement = 0, start - 1, None   # statement: where the literal's line begins
    while i >= max(0, start - 400):
        c = blank[i]
        if c in ")]}":
            level += 1
        elif c in "([{":
            if level:
                level -= 1
            elif c != "(":
                break
            elif _grouping(blank, i):
                statement = None   # the expression goes on outside the parenthesis
            else:
                return _call(text, blank, i, depth)
        elif not level and c == ";":
            break
        elif not level and c == "\n" and statement is None and not _continued(blank, i):
            statement = i + 1
        i -= 1
    return _assigned(text, blank, i + 1 if statement is None else statement, start, depth)


def _grouping(blank: str, opened: int) -> bool:
    """The parenthesis at ``opened`` groups an expression (no callee before it)."""
    word = re.search(r"([\w$]*)([)\]>]?)\s*$", blank[max(0, opened - 80):opened])
    return not (word.group(1) or word.group(2)) or word.group(1) in _KEYWORDS


_KEYWORDS = frozenset({"if", "while", "for", "switch", "return", "await", "yield", "typeof",
                       "in", "of", "and", "or", "not", "else", "case", "catch", "new"})


def _in_comment(blank: str, start: int) -> bool:
    """The literal at ``start`` sits in a line comment or a doc comment line: no request."""
    line = blank[blank.rfind("\n", 0, start) + 1:start]
    return bool(re.match(r"\s*(?:\*|/\*)", line)) or "//" in line or "#" in line


def _continued(blank: str, newline: int) -> bool:
    """The statement goes on across the line break at ``newline``."""
    before = blank[:newline].rstrip()[-1:]
    after = blank[newline:].lstrip()[:1]
    return before in tuple("=([{,?:+&|\\") or after in tuple("?:.+)&|")


def _assigned(text: str, blank: str, begin: int, start: int, depth: int) -> set[str]:
    """A literal assigned to a name: the methods of the calls that use the name."""
    m = _ASSIGNED.search(blank[begin:start])
    if not m or m.group(1) in ("return", "yield", "await"):
        return set()
    name, found = m.group(1), set()
    end = start + len(LITERAL.match(text, start).group()) if LITERAL.match(text, start) else start
    for use in list(re.finditer(rf"(?<![\w$.]){re.escape(name)}(?![\w$])",
                                blank[end:end + 4000]))[:8]:
        at = end + use.start()
        if re.match(r"\s*=(?!=)", blank[at + len(name):]):
            break   # reassigned: later uses hold another value
        found |= request_methods(text, blank, at, depth + 1)
    return found


def _call(text: str, blank: str, opened: int, depth: int) -> set[str]:
    lead = max(0, opened - 400)
    head = _without_type_arguments(blank[lead:opened])
    m = _CALLEE.search(head)
    close = _close(blank, opened)
    if not m or close is None:
        return set()
    callee = re.sub(r"\s+", "", m.group(1))
    last = callee.split(".")[-1]
    args = text[opened + 1:close]
    option = _METHOD_OPTION.search(args)
    if option:
        return {option.group(1).upper()}
    for arg in _arguments(args, blank[opened + 1:close]):
        literal = _METHOD_ARGUMENT.match(arg)
        if literal:
            return {literal.group(1).upper()}
    if last == "api":   # Graph SDK: client.api(url).select(...).get()
        pos = close + 1
        while (link := _CHAINED.match(blank, pos)) is not None:
            if link.group(1).lower() in _SDK_VERBS:
                return {_SDK_VERBS[link.group(1).lower()]}
            end = _close(blank, link.end() - 1)
            if end is None:
                break
            pos = end + 1
        return set()
    verb = _VERB_CALLEE.search(last)
    if verb:
        verb_name = (verb.group(1) or verb.group(2)).upper()
        return {"GET" if verb_name == "DOWNLOAD" else verb_name}
    if last == "fetch":
        return {"GET"}
    callee_start = lead + m.start(1)
    if last in _URL_BUILDERS:
        return request_methods(text, blank, callee_start, depth + 1)
    if callee.count(".") > int(callee.startswith(("self.", "this."))):
        return set()   # a method of another object: its definition is not read
    return _wrapper(text, blank, last, callee_start, depth)


def _wrapper(text: str, blank: str, name: str, call_at: int, depth: int) -> set[str]:
    """A call of a function defined in the same file: its default method, the method of its
    own request call, or (a helper that only builds the URL) the call around it."""
    n = re.escape(name)
    found = re.search(rf"(?:\bfunction\s*\*?\s*{n}\s*[<(]|\b(?:const|let|var)\s+{n}\s*=\s*"
                      rf"(?:async\s*)?(?:function\b|\()|\bdef\s+{n}\s*\(|^\s*(?:async\s+)?{n}\s*\()",
                      blank, re.M)
    if not found:
        return set()
    default = _DEFAULT_METHOD.search(text[found.start():found.start() + 300])
    if default:
        return {default.group(1).upper()}
    body = blank[found.end():found.end() + 2000]
    following = re.search(r"\n(?:def |class |(?:export\s+)?(?:async\s+)?function\b)", body)
    body = body[:following.start()] if following else body
    request = _REQUEST_CALL.search(body)
    if request is None:
        return request_methods(text, blank, call_at, depth + 1)
    opened = found.end() + request.end() - 1
    close = _close(blank, opened)
    if close is not None and re.search(r"\bmethod\b", text[opened:close]) \
            and not _METHOD_OPTION.search(text[opened:close]):
        return set()   # the wrapper passes a method it is given: not decidable here
    return _call(text, blank, opened, depth + 1)


def graph_operations() -> dict[str, frozenset[str]]:
    """Graph template -> the Workbench reader operations (``live_source_grammar.TEMPLATES``)
    that request it: each operation's template folded as ``graph_template`` folds upstream
    code (a drive-relative path value begins with ``/``). A live check credits a template
    only by naming one of these operations as read."""
    from integrations import live_source_grammar as grammar

    out: dict[str, set[str]] = defaultdict(set)
    for op, (host, template) in grammar.TEMPLATES.items():
        if host == grammar.GRAPH:
            path = re.sub(r"\{[a-z]+:drive_path\}", "/{}", template.removeprefix("/v1.0"))
            out[graph_template(path)[0]].add(op)
    return {template: frozenset(ops) for template, ops in out.items()}


def aligned_operations(mapped: Mapping[str, Mapping[str, Any]], root: Path = ROOT
                       ) -> dict[str, frozenset[str]]:
    """Graph template -> the reader operations a live check credits it by: those whose
    template folds to it (``graph_operations``) and those whose template resolves, against
    the pinned Graph v1.0 subset under ``root``, to the same resource as the template's
    resolution in the endpoint map ``mapped`` (``ontology.generator.graph.resource``: the
    same operation, a function by name, on the same type reached through the same type),
    e.g. a read of an item's children by id credits the listing of a folder's children by
    path. A read of another resource (a delta for a listing) credits nothing."""
    from ontology.generator import graph

    out = {template: set(ops) for template, ops in graph_operations().items()}
    csdl = root / Path(GRAPH_ENDPOINTS).parent / "graph_csdl.json"
    if not csdl.is_file() or not mapped:
        return {template: frozenset(ops) for template, ops in out.items()}
    model = graph.Csdl.from_doc(json.loads(csdl.read_text(encoding="utf-8")))
    reads: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for template, ops in out.items():
        key = graph.resource(model, template, graph.resolve(model, template))
        if key is not None:
            reads[key] |= ops
    for entry_id, item in mapped.items():
        name = entry_id.split(":", 1)[1]
        key = graph.resource(model, item.get("as", name), item.get("resolved", {}))
        if key in reads:
            out.setdefault(name, set()).update(reads[key])
    return {template: frozenset(ops) for template, ops in out.items()}


def graph_entries(inv: Inventory, tree: Tree, code: Mapping[str, str]) -> None:
    for path, text in code.items():
        for m in _SP_SITE.finditer(text):
            inv.add("sharepoint", "site", m.group(1), tree.ref(path, _line(text, m.start())))
        if not _GRAPH_FILE.search(text):
            continue
        blank = None
        for start, literal in _literals(text):
            m = _GRAPH_LITERAL.match(literal)
            if not m or m.group(1).strip("/").split("/")[0] not in GRAPH_ROOTS:
                continue
            template, sites = graph_template(m.group(1))
            ref = tree.ref(path, _line(text, start))
            blank = blank or _blank_literals(text)
            methods = (set() if _in_comment(blank, start)
                       else request_methods(text, blank, start) or {UNKNOWN_METHOD})
            inv.add("microsoft-graph", "endpoint", template, ref, resource=template.split("/")[1],
                    methods=methods)
            for site in sites:
                inv.add("sharepoint", "site", site, ref)


def external_entries(inv: Inventory, tree: Tree, code: Mapping[str, str]) -> None:
    for path, text in code.items():
        for m in _HOST.finditer(text):
            host = m.group(1).lower()
            if not _RESERVED_HOST.search(host):
                inv.add("external", "host", host, tree.ref(path, _line(text, m.start())))
    product = [p for p in tree.paths if is_product(p)]
    for path, text in tree.read(p for p in product if _ENV_EXAMPLE.search(p)).items():
        for m in _ENV_NAME.finditer(text):
            name = m.group(1)
            inv.add("external", "env_group", name.split("_")[0],
                    tree.ref(path, _line(text, m.start())), variables=[name])
    packages = tree.read(p for p in product if posixpath.basename(p) == "package.json")
    for path, text in packages.items():
        try:
            dependencies = json.loads(text).get("dependencies") or {}
        except (ValueError, AttributeError):
            continue
        for package in dependencies:
            if _SDK.search(package):
                inv.add("external", "sdk", package, tree.ref(path))


def build(trees: Mapping[str, Tree], pins: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """The registry document for ``trees`` (repo -> Tree at its pinned commit)."""
    if PORTAL not in trees:
        raise RegistryError(f"no repo-level pin for {PORTAL}")
    inv = Inventory()
    for repo in sorted(trees):
        tree = trees[repo]
        code = tree.code()
        if repo == PORTAL:
            portal_entries(inv, tree, code)
        mcp_entries(inv, tree, code)
        bc_entries(inv, tree, code)
        graph_entries(inv, tree, code)
        external_entries(inv, tree, code)
    return with_live_relations(_document(inv.finish(), trees, pins))


def live_relation_entries(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Registry entries of the relations the pinned catalog records as ``live_added``: served
    by the live ``GET /v1/relations`` but absent from the pinned manifest. Each cites the
    live read (never a manifest line) and carries only names: schema and domain."""
    block = catalog.get("live_added") or {}
    by_name = {row["name"]: row for row in catalog.get("relations", ())}
    out = []
    for name in sorted(block.get("relations", ())):
        row = by_name[name]
        entry = {"id": f"portal-reporting-api.relation:{name}", "system": "portal-reporting-api",
                 "kind": "relation", "name": name, "schema": row["schema"]}
        if row.get("domain"):
            entry["domain"] = row["domain"]
        entry["evidence"] = [f"{PORTAL}:{block['ref']} (read-only catalog; not in the pinned "
                             f"allowlist.json@{catalog['source']['commit']})"]
        out.append(entry)
    return out


def with_live_relations(registry: Mapping[str, Any],
                        catalog_path: Path = PORTAL_CATALOG_PATH) -> dict[str, Any]:
    """``registry`` plus the live-added relations of the pinned catalog (idempotent): its
    entries, counts and digest are recomputed. ``build`` ends here, so a regeneration at the
    pins and this step run on the committed registry give the same document."""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    live = live_relation_entries(catalog)
    ids = {e["id"] for e in live}
    entries = sorted([e for e in registry["entries"] if e["id"] not in ids] + live,
                     key=lambda e: e["id"])
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for entry in entries:
        counts[entry["system"]][entry["kind"]] += 1
    out = dict(registry)
    out["counts"] = {"entries": len(entries),
                     "by_system": {s: dict(sorted(k.items())) for s, k in sorted(counts.items())}}
    out["entries_digest"] = entries_digest(entries)
    out["entries"] = entries
    return out


def _document(entries: Sequence[Mapping[str, Any]], trees: Mapping[str, Tree],
              pins: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for entry in entries:
        counts[entry["system"]][entry["kind"]] += 1
    return {
        "format": FORMAT,
        "_usage": ("Generated by `python3 -m scripts.source_registry generate` from the repo-level "
                   "pins (devloop/upstream-pins.json repo_pins); never edit by hand. Coverage "
                   "(declared -> bound -> captured -> queryable -> evaluated) is computed on read: "
                   "`python3 -m scripts.source_registry status|gaps`. Fields and gap format: "
                   "scripts/source_registry.py docstring."),
        "upstream": {repo: {"commit": trees[repo].commit, "branch": pins[repo].get("branch")}
                     for repo in sorted(trees)},
        "counts": {"entries": len(entries),
                   "by_system": {s: dict(sorted(k.items())) for s, k in sorted(counts.items())}},
        "entries_digest": entries_digest(entries),
        "entries": entries,
    }


def entries_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render(registry: Mapping[str, Any]) -> str:
    """One entry per line, so a regeneration's diff names the entries that changed."""
    head = {k: v for k, v in registry.items() if k != "entries"}
    text = json.dumps(head, indent=1, ensure_ascii=False)[:-2]
    lines = [json.dumps(e, ensure_ascii=False, separators=(", ", ": ")) for e in registry["entries"]]
    return text + ',\n "entries": [\n  ' + ",\n  ".join(lines) + "\n ]\n}\n"


# --------------------------------------------------------------------------- pins and check

def repo_pins(pins: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """repo -> {commit, branch} from the pin file's ``repo_pins`` (blob-only pins skipped)."""
    out: dict[str, dict[str, str]] = {}
    for pin in pins.get("repo_pins", []):
        if not pin.get("commit") or pin.get("status") != "resolved":
            continue
        if pin["repo"] in out and out[pin["repo"]]["commit"] != pin["commit"]:
            raise RegistryError(f"two repo-level pins for {pin['repo']}")
        out[pin["repo"]] = {"commit": pin["commit"], "branch": pin.get("branch")}
    return out


def open_trees(pins_doc: Mapping[str, Any], overrides: Mapping[str, str]
               ) -> tuple[dict[str, Tree], dict[str, str]]:
    """Trees at every repo-level pin, and repo -> reason for each pin that cannot be read."""
    trees, missing = {}, {}
    for repo, pin in repo_pins(pins_doc).items():
        checkout = checkout_for(repo, dict(overrides), dict(pins_doc))
        if not Tree.holds(checkout, pin["commit"]):
            missing[repo] = (f"{checkout} lacks {pin['commit'][:12]} "
                             f"(git -C {checkout} fetch --depth 1 origin {pin['commit']})")
            continue
        trees[repo] = Tree(repo, checkout, pin["commit"])
    return trees, missing


def check(registry_path: Path = REGISTRY_PATH, pins_path: Path = PINS_PATH,
          overrides: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The gate: is the committed registry what the current pins generate?"""
    pins_doc = json.loads(pins_path.read_text(encoding="utf-8"))
    text = registry_path.read_text(encoding="utf-8") if registry_path.is_file() else None
    if text is None:
        return {"ok": False, "problems": [f"{registry_path} missing"]}
    registry = json.loads(text)
    problems = []
    if registry.get("format") != FORMAT:
        problems.append(f"format is not {FORMAT}")
    if registry.get("entries_digest") != entries_digest(registry.get("entries", [])):
        problems.append("entries differ from entries_digest: edited without regeneration")
    pinned = {repo: pin["commit"] for repo, pin in repo_pins(pins_doc).items()}
    recorded = {repo: block.get("commit") for repo, block in registry.get("upstream", {}).items()}
    stale = [{"repo": repo, "pinned": pinned.get(repo), "registry": recorded.get(repo)}
             for repo in sorted(set(pinned) | set(recorded)) if pinned.get(repo) != recorded.get(repo)]
    report: dict[str, Any] = {"ok": False, "stale_pins": stale, "problems": problems}
    if not stale and not problems:
        trees, missing = open_trees(pins_doc, overrides or {})
        if missing:
            report["reproduced"] = {"checked": False, "missing_checkouts": missing}
        else:
            same = render(build(trees, repo_pins(pins_doc))) == text
            report["reproduced"] = {"checked": True, "same": same}
            if not same:
                problems.append("a regeneration at the pinned commits differs")
    report["ok"] = not stale and not problems
    if not report["ok"]:
        report["fix"] = "python3 -m scripts.source_registry generate"
    return report


# --------------------------------------------------------------------------- coverage

# The data files ``RepoFacts`` reads under its root (a prefix ending in ``/`` is a directory);
# the capture code constants come from the running checkout.
FACT_INPUTS = ("ontology/schema/", "ontology/source_registry.json", "ontology/source_scope.json",
               "devloop/DECISIONS/", "integrations/sharepoint_capture_scope.json",
               "integrations/sharepoint_estate_scope.json", "operating/link_contracts.json")


def _registry_entries(owner: str, item: Mapping[str, Any]) -> list[str]:
    ids = item.get("registry_entries", [])
    if not isinstance(ids, list) or not all(isinstance(i, str) and ":" in i and SCOPE_KEY.match(i)
                                            for i in ids):
        raise RegistryError(f"{owner}: registry_entries must be a list of registry entry ids")
    return ids


class RepoFacts:
    """What this repository's code holds, for the stage predicates. ``root``: a directory
    holding the ``FACT_INPUTS`` files (default this checkout), so a caller can judge the
    data of another commit; its schema is format-checked by the gate where it was committed."""

    def __init__(self, root: Path = ROOT) -> None:
        from integrations import bc_read
        from integrations.capture_sharepoint import CaptureScope
        from integrations.live_source_grammar import BC_DOCUMENT_LINES
        from ontology import project
        from ontology import schema as ontology_schema

        schema_dir = root / "ontology" / "schema"
        compiled = ontology_schema.load_schema(
            schema_dir, check_format=False, link_contracts=json.loads(
                (root / "operating" / "link_contracts.json").read_text(encoding="utf-8")))
        self.bindings: dict[tuple[str, str], list[str]] = defaultdict(list)
        for binding_id, (_, binding) in sorted(compiled.bindings.items()):
            self.bindings[(binding["source"], binding["relation"])].append(binding_id)
        # A not_served type has no binding (C0): the generator's coverage names the
        # relation each one declares; it counts only while that type is not_served with
        # an upstream ask.
        coverage = json.loads((schema_dir / Path(ontology_schema.COVERAGE_PATH).relative_to(
            ontology_schema.SCHEMA_DIR)).read_text(encoding="utf-8"))
        self.unserved: dict[tuple[str, str], list[str]] = defaultdict(list)
        bc_coverage = schema_dir / "generated" / "meta" / "bc_coverage.json"
        if not bc_coverage.is_file():   # never read as "no BC set is not_served"
            raise RegistryError(f"{bc_coverage.relative_to(root)} missing: run python3 -m "
                                "scripts.gen_ontology_skeleton generate")
        for family, doc in (("portal_api", coverage),
                            ("bc", json.loads(bc_coverage.read_text(encoding="utf-8")))):
            for relation, entry in sorted(doc.get("not_served", {}).items()):
                availability = compiled.object_types.get(entry["object_type"], {}).get(
                    "availability", {})
                if availability.get("state") == "not_served" and availability.get("upstream_ask"):
                    self.unserved[(family, relation)].append(entry["object_type"])
        self.catalog = set(ontology_schema.load_catalog(schema_dir / Path(
            ontology_schema.CATALOG_SNAPSHOT_PATH).relative_to(ontology_schema.SCHEMA_DIR)).served())
        captured_sets = set(bc_read.CAPTURE_ENTITY_SETS)
        self.bc_captured = captured_sets | {bc_read.COMPANY_SET} | {
            BC_DOCUMENT_LINES[s] for s in captured_sets if s in BC_DOCUMENT_LINES}
        self.families = set(project.CAPTURE_SOURCES)
        self.captured_relations = dict(project.CAPTURED_RELATIONS)
        scope = json.loads((root / "integrations" / "sharepoint_capture_scope.json")
                           .read_text(encoding="utf-8"))
        self.sharepoint_sites = {t["site"].casefold() for t in scope.get("targets", []) if t.get("site")}
        # The estate scope's discover rule (the capture's own parser): every site on the
        # configured host is captured unless its exclusion list names the whole site.
        estate = root / "integrations" / "sharepoint_estate_scope.json"
        self.sharepoint_discover = (CaptureScope.from_json(json.loads(
            estate.read_text(encoding="utf-8"))).discover if estate.is_file() else None)
        self.binding_source = {bid: binding["source"]
                               for bid, (_, binding) in compiled.bindings.items()}
        # Registry entry id -> (binding or action type id, data key) of each declaring it.
        self.declared: dict[str, list[tuple[str, tuple[str, str]]]] = defaultdict(list)
        for binding_id, (_, binding) in sorted(compiled.bindings.items()):
            for entry_id in _registry_entries(binding_id, binding):
                self.declared[entry_id].append((binding_id, (binding["source"], binding["relation"])))
        self.actions: dict[tuple[str, str], bool] = {}   # action key -> its target is served
        for action_id, action in sorted(compiled.action_types.items()):
            ids = _registry_entries(action_id, action)
            if ids:
                target = compiled.object_types.get(action["target"], {})
                self.actions[(ACTION, action_id)] = \
                    target.get("availability", {}).get("state") != "not_served"
            for entry_id in ids:
                self.declared[entry_id].append((action_id, (ACTION, action_id)))
        # Graph templates: the generated endpoint map's family/relation, else why not (no
        # map: every template stays unbound).
        self.not_business_data: dict[str, tuple[str, str]] = {}
        self.unresolved: dict[str, str] = {}
        self.method_unknown: dict[str, str] = {}
        self.blocked: dict[tuple[str, str], tuple[str, str]] = {}   # key -> (reason, decision)
        graph_map = root / GRAPH_ENDPOINTS
        graph = (json.loads(graph_map.read_text(encoding="utf-8")) if graph_map.is_file()
                 else {"endpoints": {}})
        for entry_id, item in sorted(graph["endpoints"].items()):
            if "source" in item:
                key = (item["source"], item["relation"])
                self.declared[entry_id].append((GRAPH_ENDPOINTS, key))
                if "blocked" in item:
                    held = (item["blocked"]["reason"], item["blocked"]["decision"])
                    if self.blocked.setdefault(key, held) != held:
                        raise RegistryError(f"{GRAPH_ENDPOINTS}: {key[0]}.{key[1]} is blocked "
                                            "for two reasons")
            elif "not_business_data" in item:
                self.not_business_data[entry_id] = (item["not_business_data"], item["decision"])
            elif "method_unknown" in item:
                self.method_unknown[entry_id] = item["method_unknown"]
            else:
                self.unresolved[entry_id] = item["unresolved"]
        for key in sorted(self.blocked):
            if self._capture_gap(key) is None:
                raise RegistryError(f"{GRAPH_ENDPOINTS}: {key[0]}.{key[1]} is declared blocked "
                                    "but a capture reads it: drop its blocked declaration "
                                    "(ontology/generator/graph_families.json) and regenerate")
        # Registry system -> the source families its declared entries are bound under.
        self.system_families: dict[str, set[str]] = defaultdict(set)
        for entry_id, owners in self.declared.items():
            self.system_families[entry_id.split(".", 1)[0]].update(
                family for _owner, (family, _relation) in owners if family != ACTION)
        self.graph_operations = aligned_operations(graph["endpoints"], root)
        self.scope = load_scope(root)
        record = root / AUTH_LAYER_DECISION
        self.auth_layer_decided = record.is_file() and bool(
            DECIDED.search(record.read_text(encoding="utf-8")))
        self.fixture_rows, self.fixture = fixture_probe(compiled)

    def bound(self, key: tuple[str, str]) -> bool:
        return key in self.bindings or self.actions.get(key, False)

    def site_in_scope(self, name: str) -> bool:
        """A SharePoint site a capture scope covers: named by a target, or found by the
        estate's discover rule and not excluded by it."""
        return name.casefold() in self.sharepoint_sites or (
            self.sharepoint_discover is not None
            and not self.sharepoint_discover.site_excluded(name))

    def unserved_key(self, key: tuple[str, str]) -> bool:
        return key in self.unserved or self.actions.get(key) is False

    def decision(self, entry: Mapping[str, Any]) -> tuple[str, str] | None:
        """(scope, decision path) that puts the entry out of scope, if any."""
        for scope in (entry["id"], f"{entry['system']}.{entry['kind']}"):
            if scope in self.scope:
                return scope, self.scope[scope]
        return None

    def fixture_queryable(self, key: tuple[str, str]) -> bool:
        return all(self.fixture.get(bid, "not_probed") is None for bid in self.bindings[key])

    def captured(self, key: tuple[str, str]) -> str | None:
        why = self._capture_gap(key)
        return "blocked" if why == "not_captured" and key in self.blocked else why

    def _capture_gap(self, key: tuple[str, str]) -> str | None:
        family, relation = key
        if family == "portal_api":
            return None if relation in self.catalog else "not_in_catalog"
        if family == "bc":
            return None if relation in self.bc_captured else "not_captured"
        held = self.captured_relations.get(family)
        if held is not None and relation not in held:
            return "not_captured"
        return None if family in self.families else "not_captured"


FIXTURE_AT = "2026-01-03T00:00:00Z"
FIXTURE_PRINCIPAL = "fixture:probe"
FIXTURE_PAGES = 20


def fixture_probe(compiled: Any, rows: int | None = None) -> tuple[int, dict[str, str | None]]:
    """(rows per relation, binding id -> None when synthetic rows of the binding came back
    through ``plx.find`` and ``plx.object``, else why not). Local and in memory: a
    temporary store, never a live read, never a written result."""
    import datetime as dt
    import tempfile

    from ontology import store_ddl
    from ontology.project import build_store
    from ontology.generator import fixture
    from ontology.types import Actor
    from operating import ontology_tools as ot

    rows = rows or fixture.min_rows(compiled)
    at = dt.datetime.fromisoformat(FIXTURE_AT.replace("Z", "+00:00"))
    out: dict[str, str | None] = {}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fixture.sqlite"
        build_store(path, compiled, fixture.sources(compiled, rows), clock=lambda: at)
        reader = ot.StoreReader.open(path)
        try:
            who = frozenset({FIXTURE_PRINCIPAL})
            policy = ot.ReadPolicy(tenant="plx", audiences={k: who for k in reader.audience_keys()},
                                   markings={m: who for m in store_ddl.MARKINGS})
            tools = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(compiled),
                                     grammar=ot.default_grammar(), clock=lambda: at,
                                     item_gate=lambda _actor, handle:
                                     fixture.is_synthetic(handle))
            actor = Actor("plx", who)
            for bid, (type_id, _) in sorted(compiled.bindings.items()):
                try:
                    out[bid] = probe_binding(tools, actor, compiled.object_types[type_id], bid)
                except Exception as exc:  # noqa: BLE001 - a failing probe is a gap, not a crash
                    out[bid] = "probe_error:" + type(exc).__name__
        finally:
            reader.conn.close()
    return rows, out


def probe_binding(tools: Any, actor: Any, spec: Mapping[str, Any], bid: str) -> str | None:
    if spec.get("availability", {}).get("state") == "not_served":
        return "not_served"
    names = [p["name"] for p in spec.get("properties", ())
             if any(s.get("binding") == bid for s in p["sources"])]
    if not names:
        return "no_property"
    for name in names:
        args: dict[str, Any] = {"object_type": spec["id"], "limit": 50,
                                "filters": [{"property": name, "op": "present"}]}
        for _ in range(FIXTURE_PAGES):
            found = tools.call("plx.find", args, actor)
            if "error" in found:
                return "find_error:" + str(found["error"].get("code"))
            ids = [c["object"]["object_id"] for c in found.get("candidates", ())
                   if c["object"]["object_id"].split(":", 2)[1] == bid]
            if ids:
                read = tools.call("plx.object", {"object_id": ids[0]}, actor)
                if "error" in read:
                    return "object_error:" + str(read["error"].get("code"))
                return None if read.get("values") else "object_without_values"
            cursor = found.get("completeness", {}).get("next_cursor")
            if not cursor:
                break
            args = dict(args, cursor=cursor)
    return "not_found"


# Entry kind -> the data keys its name and attributes name (``tables``: Prisma model -> table).
KIND_KEYS = {
    "relation": lambda e, _t: [("portal_api", e["name"])],
    "prisma_model": lambda e, _t: [("portal_api", "public." + e["table"])],
    "api_route": lambda e, t: [("portal_api", "public." + t[m]) for m in e.get("models", ())
                               if m in t] + [("portal_api", r) for r in e.get("relations", ())],
    "entity_set": lambda e, _t: [("bc", e["name"])],
    "api_page": lambda e, _t: [("bc", e["name"])],
    "site": lambda e, _t: [("sharepoint", "driveItems")],
}


def data_keys(entry: Mapping[str, Any], tables: Mapping[str, str],
              declared: Mapping[str, Sequence[tuple[str, tuple[str, str]]]] | None = None
              ) -> list[tuple[str, str]]:
    """The (source family, relation) pairs an entry's data is bound under, if any: those
    its kind names (``KIND_KEYS``) and those of the bindings and action types that declare
    its id (``RepoFacts.declared``), for every kind."""
    named = KIND_KEYS.get(entry["kind"], lambda _e, _t: [])(entry, tables)
    return sorted(set(named) | {key for _, key in (declared or {}).get(entry["id"], ())})


def _key_names(key: tuple[str, str]) -> set[str]:
    family, relation = key
    if family == "portal_api" and relation.startswith("public."):
        return {relation, relation[len("public."):]}
    return {relation}


def load_live(files: Sequence[Path] | None = None, ref: str = LIVE_REF) -> list[dict[str, Any]]:
    """Live result documents: ``files``, else the latest hourly and daily runs on ``ref``."""
    docs = []
    if files is not None:
        for path in files:
            docs.append((str(path), json.loads(Path(path).read_text(encoding="utf-8"))))
    else:
        for name in LIVE_FILES:
            done = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{name}"],
                                  capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
            if done.returncode == 0:
                docs.append((f"{ref}:{name}", json.loads(done.stdout)))
    return [dict(doc, _from=origin) for origin, doc in docs]


def _strings(value: Any, out: set[str]) -> set[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k))
            _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)
    elif isinstance(value, str):
        out.add(value)
    return out


def registry_names(specs_dir: Path = SPECS_DIR) -> dict[str, Mapping[str, Sequence[str]]]:
    """Check id -> its spec's ``registry_names`` (``devloop/live_verify.py`` validates it)."""
    out = {}
    for path in sorted(specs_dir.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        if "registry_names" in spec:
            out[spec["id"]] = spec["registry_names"]
    return out


class LiveIndex:
    """The newest result of every live check, with the names its result fields carry.

    A check whose spec declares ``registry_names`` credits a name only under those
    recorded paths, one outcome per name: under ``pass`` the check's result (a pass only
    when the check passed), under ``fail`` a failure, under ``unexercised``
    ``UNEXERCISED`` (read but empty); a name under any other path is not credited. Any
    other check credits every name its fields carry with its result. ``families``
    (``RepoFacts.system_families``) widens each check's alignment from the systems its
    source names to the families their entries are bound under."""

    def __init__(self, docs: Sequence[Mapping[str, Any]],
                 families: Mapping[str, set[str]] | None = None) -> None:
        declared = registry_names()
        self.sources = [{"from": d["_from"], "run": d.get("run"), "generated_at": d.get("generated_at")}
                        for d in docs]
        newest: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for doc in docs:
            stamp = doc.get("generated_at") or ""
            for chk in doc.get("checks", []):
                if chk.get("id") and (chk["id"] not in newest or newest[chk["id"]][0] <= stamp):
                    newest[chk["id"]] = (stamp, chk)
        self.checks = []
        self.called: dict[str, frozenset[str]] = {}
        for check_id, (_, chk) in sorted(newest.items()):
            aligned = set().union(*(LIVE_SOURCES.get(t, set())
                                    for t in str(chk.get("source", "")).split("+")))
            aligned |= set().union(*((families or {}).get(s, set()) for s in aligned))
            fields = ((chk.get("detail") or {}).get("fields")) or {}
            self.called[check_id] = frozenset(_strings(fields.get("tools_called"), set()))
            result, kinds = chk.get("result"), declared.get(check_id)
            if kinds is None:
                carried = dict.fromkeys(_strings(fields, set()), result)
            else:
                carried = {}
                for kind, outcome in (("unexercised", UNEXERCISED), ("pass", result),
                                      ("fail", "fail")):   # a later kind wins a shared name
                    for path in kinds.get(kind, ()):
                        carried.update(dict.fromkeys(_strings(fields.get(path), set()), outcome))
            self.checks.append((check_id, aligned, carried))

    def naming(self, scope: str, names: set[str]) -> list[tuple[str, str]]:
        """(check, outcome) of each aligned check naming any of ``names``: a failure
        before ``UNEXERCISED`` before a pass."""
        out = []
        for cid, aligned, carried in self.checks:
            found = {carried[n] for n in names if n in carried} if scope in aligned else set()
            if found:
                bad = sorted(str(r) for r in found - {"pass", UNEXERCISED})
                out.append((cid, bad[0] if bad else UNEXERCISED if UNEXERCISED in found
                            else "pass"))
        return out


def entry_status(entry: Mapping[str, Any], facts: RepoFacts, live: LiveIndex | None,
                 tables: Mapping[str, str]) -> dict[str, Any]:
    decided = facts.decision(entry)
    if decided:
        return {"status": "out_of_scope", "scope": decided[0], "decision": decided[1]}
    keys = data_keys(entry, tables, facts.declared)
    via_shared = bool(entry.get("models_via_shared") or entry.get("relations_via_shared"))
    unresolved = entry.get("unresolved_relations")
    pathed = entry.get("search_path_relations")
    pathed_reason = ("search_path_unresolved" if (entry.get("search_path_database") or {}).get(
        "catalog") else "not_resolvable")
    if not keys and entry["kind"] == "api_route" and not (
            entry.get("data_clients") or unresolved or pathed or entry.get("models")
            or entry.get("relations")):
        if not via_shared:
            return {"status": "not_a_data_source"}
        if facts.auth_layer_decided:
            return {"status": "auth_layer_only", "decision": AUTH_LAYER_DECISION}
    out: dict[str, Any] = {"status": "declared"}
    named: list[tuple[str, str]] = []
    per_target: list[list[tuple[str, str]]] = []
    all_pass = False
    if live is not None:
        if entry["kind"] == "site" or not keys:
            targets = [(keys[0][0] if keys else entry["system"], {entry["name"]})]
        elif entry["kind"] == "endpoint":   # one template of a relation many templates bind
            ops = set(facts.graph_operations.get(entry["name"], ()))
            targets = [(family, ops) for family, _ in keys if family != ACTION]
        else:
            targets = [(family, _key_names((family, rel))) for family, rel in keys
                       if family != ACTION]
        per_target = [live.naming(scope, names) for scope, names in targets]
        if entry["kind"] == "mcp_tool":   # credited only by a run that called the tool
            via_other = sorted({n for found in per_target for n in found
                                if entry["name"] not in live.called[n[0]]})
            if via_other:
                out["bound_data_read"] = [
                    {"check": c, "result": r, "read": _bound_read(live.called[c])}
                    for c, r in via_other[:MAX_EVIDENCE]]
            per_target = [[n for n in found if entry["name"] in live.called[n[0]]]
                          for found in per_target]
        named = sorted({n for found in per_target for n in found})
        all_pass = all(any(r == "pass" for _, r in found) for found in per_target)
    if named:
        out["evaluated_by"] = [{"check": c, "result": r} for c, r in named[:MAX_EVIDENCE]]

    def fail(stage: str, reason: str, blocking: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
        out.update(missing=stage, reason=reason)
        blocking = [f"{f}:{r}" for f, r in blocking][:MAX_EVIDENCE]
        if blocking and entry["kind"] == "api_route":
            out["blocking"] = blocking
        return out

    if not keys and entry["id"] in facts.not_business_data:
        out["not_business_data"], out["decision"] = facts.not_business_data[entry["id"]]
        return fail("bound", "not_business_data")
    if not keys and entry["id"] in facts.unresolved:
        out["unresolved"] = facts.unresolved[entry["id"]]
        return fail("bound", "graph_unresolved")
    if not keys and entry["id"] in facts.method_unknown:
        out["method_unknown"] = facts.method_unknown[entry["id"]]
        return fail("bound", "graph_method_unknown")
    if not keys:
        return fail("bound", "no_source_family" if entry["kind"] != "api_route" else
                    pathed_reason if pathed else
                    "reaches_data_client" if entry.get("data_clients") else
                    "unresolved_relation" if unresolved else
                    "models_via_shared" if via_shared else "no_binding")
    unbound = [k for k in keys if not facts.bound(k)]
    if unbound:
        return fail("bound", "not_served" if all(facts.unserved_key(k) for k in unbound)
                    else "no_binding", unbound)
    if unresolved or pathed:
        return fail("bound", "unresolved_relation" if unresolved else pathed_reason)
    keys = [k for k in keys if k[0] != ACTION]   # an action is bound, never captured
    if not keys:
        return {"status": "bound_action"}
    out["status"] = "bound"
    gaps = [(k, facts.captured(k)) for k in keys]
    gaps = [(k, why) for k, why in gaps if why]
    if gaps:
        if gaps[0][1] == "blocked":
            out["blocked"], out["decision"] = facts.blocked[gaps[0][0]]
        return fail("captured", gaps[0][1], [k for k, _ in gaps])
    if entry["kind"] == "site" and not facts.site_in_scope(entry["name"]):
        return fail("captured", "out_of_scope")
    out["status"] = "captured"
    unprojected = [k for k in keys if k[0] not in facts.families]
    if unprojected:
        return fail("queryable", "not_projected", unprojected)
    out["status"] = "queryable"
    unprobed = [k for k in keys if not facts.fixture_queryable(k)]
    if unprobed:
        return fail("fixture_queryable", "fixture_not_queryable", unprobed)
    out["status"] = "fixture_queryable"
    if live is None:
        return fail("evaluated", "no_live_results")
    if not all_pass:
        open_targets = [found for found in per_target if not any(r == "pass" for _, r in found)]
        if any(r != UNEXERCISED for found in open_targets for _, r in found):
            return fail("evaluated", "live_not_passing")
        if all(open_targets):
            return fail("evaluated", "live_unexercised")
        if "bound_data_read" in out:
            return fail("evaluated", "bound_data_read")
        if entry["kind"] == "endpoint":
            out["waits_for"] = ("operations_read" if facts.graph_operations.get(entry["name"])
                                else "reader_operation")
        return fail("evaluated", "no_live_check")
    out["status"] = "evaluated"
    return out


def _bound_read(called: frozenset[str]) -> str:
    """How a check read an MCP tool's bound data without calling the tool."""
    if called:
        return f"through another tool: its run called {', '.join(sorted(called))}"
    return "a direct read: its run called no MCP tool"


def coverage(registry: Mapping[str, Any] | None = None, live: Sequence[Mapping[str, Any]] | None = None,
             facts: RepoFacts | None = None) -> list[dict[str, Any]]:
    """Every registry entry with its computed stages (``live=None``: none read)."""
    registry = registry if registry is not None else load_registry()
    facts = facts or RepoFacts()
    index = LiveIndex(live, facts.system_families) if live else None
    tables = {e["name"]: e["table"] for e in registry["entries"] if e["kind"] == "prisma_model"}
    return [dict(entry, **entry_status(entry, facts, index, tables)) for entry in registry["entries"]]


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def gaps(registry: Mapping[str, Any] | None = None, live: Sequence[Mapping[str, Any]] | None = None,
         facts: RepoFacts | None = None) -> dict[str, Any]:
    """The ``plx-source-gaps/v1`` document (see the module docstring)."""
    registry = registry if registry is not None else load_registry()
    facts = facts or RepoFacts()
    rows = coverage(registry, live, facts)
    by_status: dict[str, int] = {stage: 0 for stage in STAGES + TERMINAL}
    by_system: dict[str, dict[str, int]] = defaultdict(
        lambda: {stage: 0 for stage in STAGES + TERMINAL})
    for row in rows:
        by_status[row["status"]] += 1
        by_system[row["system"]][row["status"]] += 1
    tables = {e["name"]: e["table"] for e in registry["entries"] if e["kind"] == "prisma_model"}
    read = {key for e in registry["entries"] for key in data_keys(e, tables, facts.declared)}
    covered: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["kind"] not in SCHEMA_KINDS or row["status"] in TERMINAL:
            continue
        keys = data_keys(row, tables, facts.declared)
        family = next((f for f, _ in keys if f != ACTION), SCHEMA_KINDS[row["kind"]])
        block = covered.setdefault(family, {"entries": 0, "reached": {s: 0 for s in STAGES}})
        block["entries"] += 1
        for stage in STAGES[:STAGES.index(row["status"]) + 1]:
            block["reached"][stage] += 1
    fixture: dict[str, dict[str, Any]] = {}
    for bid, why in sorted(facts.fixture.items()):
        block = fixture.setdefault(facts.binding_source[bid], {
            "bindings": 0, "fixture_queryable": 0, "not_queryable": {}})
        block["bindings"] += 1
        if why is None:
            block["fixture_queryable"] += 1
        else:
            block["not_queryable"][bid] = why
    keep = ("id", "system", "kind", "name", "status", "missing", "reason", "blocking",
            "unresolved_relations", "search_path", "search_path_relations", "search_path_database",
            "not_business_data",
            "decision", "unresolved", "method_unknown", "blocked", "bound_data_read", "waits_for")
    uncovered = [dict({k: row[k] for k in keep if k in row}, evidence=row["evidence"][0])
                 for row in sorted(rows, key=lambda r: (r["system"], r["kind"], r["id"]))
                 if row["status"] != "evaluated" and row["status"] not in TERMINAL]
    held: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["status"] == "out_of_scope":
            held[row["scope"]] += 1
    known = {e["id"] for e in registry["entries"]}
    return {
        "format": GAPS_FORMAT,
        "registry": {"path": str(REGISTRY_PATH.relative_to(ROOT)),
                     "entries_digest": registry["entries_digest"],
                     "upstream": {r: b["commit"] for r, b in registry["upstream"].items()}},
        "live": LiveIndex(live).sources if live else [],
        "counts": {"entries": len(rows), "by_status": by_status,
                   "by_system": {s: by_system[s] for s in sorted(by_system)},
                   "coverage": {"kinds": sorted(SCHEMA_KINDS),
                                "entries": sum(b["entries"] for b in covered.values()),
                                "by_family": dict(sorted(covered.items()))}},
        "fixture": {"evidence": "synthetic (ontology/generator/fixture.py), never live",
                    "rows_per_relation": facts.fixture_rows,
                    "by_family": dict(sorted(fixture.items()))},
        "out_of_scope": [{"scope": scope, "decision": decision, "entries": held[scope]}
                         for scope, decision in sorted(facts.scope.items())],
        "uncovered": uncovered,
        "bound_without_entry": [{"binding": bid, "source": source, "relation": relation}
                                for (source, relation), ids in sorted(facts.bindings.items())
                                if (source, relation) not in read for bid in ids] + [
            {"binding": bid, "source": key[0], "relation": key[1], "registry_entry": entry_id}
            for entry_id, owners in sorted(facts.declared.items()) if entry_id not in known
            for bid, key in owners],
    }


# --------------------------------------------------------------------------- CLI

def _overrides(values: Sequence[str]) -> dict[str, str]:
    out = {}
    for value in values or ():
        repo, sep, path = value.partition("=")
        if not sep:
            raise SystemExit(f"--checkout wants REPO=PATH, got {value!r}")
        out[repo] = path
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 unless the committed registry is what the current pins generate")
    parser.add_argument("--checkout", action="append", default=[], metavar="REPO=PATH")
    sub = parser.add_subparsers(dest="command")
    g = sub.add_parser("generate", help="extract the registry at the repo-level pins")
    g.add_argument("--checkout", action="append", default=[], dest="generate_checkout",
                   metavar="REPO=PATH")
    g.add_argument("--check", action="store_true", dest="generate_check",
                   help="the top-level --check: write nothing")
    sub.add_parser("add-live-relations", help="add the pinned catalog's live-added relations to "
                   "the committed registry (no Portal checkout; what generate ends with)")
    for name in ("status", "gaps"):
        p = sub.add_parser(name, help=f"computed coverage ({name})")
        p.add_argument("--live", action="append", type=Path, metavar="FILE",
                       help="a live result document (default: the latest on claude/devloop-live)")
        p.add_argument("--no-live", action="store_true", help="read no live results")
        if name == "status":
            p.add_argument("--json", action="store_true", help="every entry with its stages")
        else:
            p.add_argument("--out", type=Path, help="write the gaps document here")
    args = parser.parse_args(argv)
    if args.check or getattr(args, "generate_check", False):
        report = check(overrides=_overrides(args.checkout + getattr(args, "generate_checkout", [])))
        print(json.dumps(report, indent=1))
        return 0 if report["ok"] else 1
    if args.command == "add-live-relations":
        registry = with_live_relations(load_registry())
        REGISTRY_PATH.write_text(render(registry), encoding="utf-8")
        print(json.dumps({"ok": True, "counts": registry["counts"]}, indent=1))
        return 0
    if args.command == "generate":
        pins_doc = json.loads(PINS_PATH.read_text(encoding="utf-8"))
        trees, missing = open_trees(pins_doc, _overrides(args.checkout + args.generate_checkout))
        if missing:
            print(json.dumps({"ok": False, "missing_checkouts": missing}, indent=1))
            return 2
        registry = build(trees, repo_pins(pins_doc))
        REGISTRY_PATH.write_text(render(registry), encoding="utf-8")
        print(json.dumps({"ok": True, "path": str(REGISTRY_PATH.relative_to(ROOT)),
                          "counts": registry["counts"]}, indent=1))
        return 0
    if args.command in ("status", "gaps"):
        live = None if args.no_live else load_live(args.live)
        facts = RepoFacts()
        doc = gaps(live=live, facts=facts)
        if args.command == "gaps":
            text = json.dumps(doc, indent=1) + "\n"
            if args.out:
                args.out.write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
        elif args.json:
            json.dump(coverage(live=live, facts=facts), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            print(json.dumps({"live": doc["live"], "counts": doc["counts"],
                              "fixture": doc["fixture"]}, indent=1))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
