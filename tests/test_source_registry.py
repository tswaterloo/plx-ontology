"""The source registry: extracted from pinned upstream commits, gated, coverage computed.

Evidence label: ``isolated-fixture`` (a temporary local git repo stands in for the Portal;
coverage uses this repository's real schema, capture code and scopes with synthetic live
results). No network.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ontology import json_contract
from scripts import source_registry as sr

PORTAL = "plx-customer-portal"
FILES = {
    "portal/prisma/schema.prisma": (
        'model SalesOrder {\n  id String @id\n  @@map("app_sales_order")\n}\n'
        'model Widget {\n  id String @id\n  @@map("app_widget")\n}\n'
        'model User {\n  id String @id\n  @@map("app_user")\n}\n'),
    "services/workbench-api/manifest/allowlist.json": json.dumps({"entries": [
        {"schema": "public", "relation": "app_sales_order", "domain": "orders", "columns": ["id"]},
        {"schema": "sage", "relation": "oe_order_header", "domain": "orders", "columns": ["id"]}]}),
    "portal/tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}',
    "portal/src/lib/db.ts": 'import { PrismaClient } from "@prisma/client";\nexport const db = new PrismaClient();\n',
    "portal/src/lib/data/index.ts": '// The client, re-exported.\nexport * from "../db";\n',
    "portal/src/lib/auth.ts": 'import { db } from "./data";\nexport const who = () => db.user.findFirst({});\n',
    "portal/src/lib/pg.ts": 'import pg from "pg";\nexport const query = (sql) => new pg.Pool().query(sql);\n',
    # The database the reporting manifest's replica holds, which the catalogs describe: the one
    # its address line names, not the first the prose names; a line naming a variable with
    # only that database pins a client the variable overrides to it.
    "docs/runbooks/WORKBENCH-READONLY-DB-ACCESS.md": (
        "# Workbench read-only database access\n"
        "The admin URL is key `DATABASE_URL`; its path may name database `command_center`.\n"
        "Consumers connect to the replica only:\n"
        "`replica.example.test`, port 5432, database `plxportal`.\n"
        "The API reads `WB_API_DATABASE_URL`, set to database `plxportal`.\n"),
    # A pg client of another database, by default (an environment variable overrides it), that
    # sets its own search path (in a string; the comment is not one).
    "portal/src/lib/fm-db.ts": ('import pg from "pg";\nconst FM_DEFAULT_DATABASE = "filemaker_replica";\n'
                                "const database = process.env.FM_DATABASE_NAME ?? FM_DEFAULT_DATABASE;\n"
                                '// set search_path to v2 first, then public\n'
                                'export const queryFM = async (sql) => { const c = new pg.Pool();\n'
                                '  await c.query("SET search_path TO v2, public"); return c.query(sql); };\n'),
    "portal/src/lib/fm/index.ts": 'export * from "../fm-db";\n',
    # Auth plus a pg client of its own: a table no model or relation resolves is kept; a CTE,
    # a function and a literal holding no statement are not tables.
    "portal/src/app/api/legacy/route.ts": (
        'import { who } from "@/lib/auth";\nimport { query } from "@/lib/pg";\n'
        'const help = "Rows FROM legacy";\n'
        'export const GET = () => query("WITH recent AS (SELECT id FROM purchase_order) "\n'
        '  + "SELECT id FROM recent JOIN unnest($1) u ON true");\n'),
    # SQL through the search-path client (here through a re-export module): an unqualified
    # table, even one a model or a reporting relation of the same name maps, is not resolved
    # while v2, ahead of public on the path, is in no catalog;
    # a qualified one and Prisma raw SQL in the same module are.
    "portal/src/app/api/fm/po/route.ts": (
        'import { who } from "@/lib/auth";\nimport { queryFM } from "@/lib/fm";\nimport { db } from "@/lib/db";\n'
        'export const GET = () => queryFM("SELECT id FROM purchase_order JOIN app_sales_order s ON true '
        'JOIN sage.oe_order_header h ON true");\n'
        'export const POST = () => db.$executeRaw(Prisma.sql`UPDATE "app_widget" SET x = 1`);\n'),
    "portal/src/lib/orders.ts": ('import { db } from "@/lib/db";\n'
                                 "export const list = () => db.salesOrder.findMany({});\n"
                                 'export const legacy = () => q("SELECT id FROM sage.oe_order_header");\n'),
    "portal/src/app/api/orders/[id]/route.ts": ('import { who } from "@/lib/auth";\n'
                                                'import { list } from "@/lib/orders";\n'
                                                "export async function GET() {}\n"
                                                "export const PATCH = async () => {};\n"),
    "portal/src/app/api/widgets/route.ts": ('import { who } from "@/lib/auth";\nimport { db } from "@/lib/db";\n'
                                            "const handler = () => db.widget.create({});\n"
                                            "export { handler as POST };\n"),
    "portal/src/app/api/quality/tests/route.ts": ('import { who } from "@/lib/auth";\n'
                                                  "export function GET() {}\n"),
    "portal/src/app/api/health/route.ts": "export function GET() {}\n",
    # Reads whichever model it is asked for: no model name in any literal; the client comes
    # through a shared re-export module.
    "portal/src/app/api/query/route.ts": ('import { db } from "@/lib/data";\n'
                                          "export const GET = (m) => db[getDelegate(m)].findMany({});\n"),
    "portal/src/lib/business-central/sync.ts": (
        "export const lines = (seg, id) => bcRequest(`${seg}/salesOrders(${id})/salesOrderLines`);\n"
        'export const vendors = (c) => listCompanyEntity(c, "vendors");\n'),
    "portal/src/lib/m365/graph.ts": (
        "export const inbox = (m) => graphFetch(`/users/${m}/messages`);\n"
        'export const send = () => graphFetch("/users/someone@petrasoap.com/sendMail");\n'
        'export const SITE = "https://petrasoap.sharepoint.com/sites/PLXCUSTOMERS";\n'),
    # The HTTP method of each Graph request, read from the call that sends it.
    "portal/src/lib/m365/drive.ts": (
        'async function graph(path, method = "GET", body) {\n'
        "  return fetch(`https://graph.microsoft.com/v1.0${path}`, { method, body });\n}\n"
        'export const item = (d, i) => client.api(`/drives/${d}/items/${i}`).select("id").get();\n'
        "export const upload = (d, p, b) => client.api(`/drives/${d}/root:/${p}:/content`).put(b);\n"
        "export async function folder(d, p, name) {\n"
        "  const parent = p\n"
        "    ? `/drives/${d}/root:/${p}:/children`\n"
        "    : `/drives/${d}/root/children`;\n"
        '  await graph(parent, "POST", { name });\n}\n'
        "export const children = (d, i) => graph(`/drives/${d}/items/${i}/children`);\n"
        "export const tag = (d, i) => fetch(\n"
        '  `https://graph.microsoft.com/v1.0/drives/${d}/items/${i}/listItem/fields`, { method: "PATCH" });\n'
        "// a comment names no request: `/drives/${d}/items/${i}/thumbnails`\n"),
    "portal/src/lib/mcp/server.ts": ('const server = new McpServer({ name: "PLX-MC", version: "1" });\n'
                                     'server.tool("mc_get_task", "Read one task.", {}, run);\n'),
    "portal/src/lib/mcp/server.test.ts": 'server.tool("mc_test_only", "", {}, run);\n',
    "portal/.env.example": "RESEND_API_KEY=re_live_secret_value\n# DOCUSIGN_BASE_URL=https://demo.docusign.net\n",
    "portal/package.json": json.dumps({"dependencies": {"@aws-sdk/client-s3": "3", "react": "19"}}),
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True, text=True).stdout.strip()


class UpstreamFixture(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.up = self.tmp / PORTAL
        self.up.mkdir()
        _git(self.up, "init", "-q")
        for path, text in FILES.items():
            (self.up / path).parent.mkdir(parents=True, exist_ok=True)
            (self.up / path).write_text(text)
        _git(self.up, "add", ".")
        _git(self.up, "commit", "-qm", "base")
        self.commit = _git(self.up, "rev-parse", "HEAD")
        self.pins = self.tmp / "pins.json"
        self.pin(self.commit)
        self.overrides = {PORTAL: str(self.up)}

    def pin(self, commit: str) -> None:
        self.pins.write_text(json.dumps({"repo_pins": [
            {"repo": PORTAL, "branch": "staging", "commit": commit, "status": "resolved"},
            {"repo": "PLX_MC", "blob": "d59fc77f", "status": "resolved"}]}))

    def build(self) -> dict:
        pins_doc = json.loads(self.pins.read_text())
        trees, missing = sr.open_trees(pins_doc, self.overrides)
        self.assertEqual(missing, {})
        return sr.build(trees, sr.repo_pins(pins_doc))


class GenerateTests(UpstreamFixture):
    def test_entries_are_extracted_with_pinned_evidence_and_no_values(self):
        registry = self.build()
        text = sr.render(registry)
        self.assertEqual(text, sr.render(self.build()), "regeneration must be byte-identical")
        by_id = {e["id"]: e for e in registry["entries"]}
        orders = by_id["portal.api_route:portal/src/app/api/orders/[id]"]
        self.assertEqual((orders["name"], orders["methods"]), ("/api/orders/[id]", ["GET", "PATCH"]))
        # Data reached through imports; the auth module every route imports is not attributed.
        self.assertEqual((orders["models"], orders["relations"]), (["SalesOrder"], ["sage.oe_order_header"]))
        widgets = by_id["portal.api_route:portal/src/app/api/widgets"]
        self.assertEqual((widgets["methods"], widgets["models"]), (["POST"], ["Widget"]))
        # No data of its own: what the skipped shared layer reaches is reported, not attributed.
        quality = by_id["portal.api_route:portal/src/app/api/quality/tests"]
        self.assertEqual((quality["models"], quality["models_via_shared"]), ([], ["User"]))
        self.assertNotIn("data_clients", quality)   # the auth layer's own client is not its
        legacy = by_id["portal.api_route:portal/src/app/api/legacy"]
        self.assertEqual((legacy["models_via_shared"], legacy["data_clients"],
                          legacy["unresolved_relations"]),
                         (["User"], ["portal/src/lib/pg.ts"], ["purchase_order"]))
        self.assertNotIn("search_path", legacy)
        po = by_id["portal.api_route:portal/src/app/api/fm/po"]
        self.assertEqual((po["models"], po["relations"], po["search_path"], po["search_path_relations"]),
                         (["Widget"], ["sage.oe_order_header"], "v2, public",
                          ["app_sales_order", "purchase_order"]))
        self.assertEqual(po["search_path_database"], {
            "default": "filemaker_replica", "override": ["FM_DATABASE_NAME"], "evidence": [
                f"{PORTAL}:portal/src/lib/fm-db.ts@{self.commit[:12]}:{n}" for n in (2, 3, 6)]})
        self.assertNotIn("unresolved_relations", po)
        self.assertNotIn("models_via_shared", by_id["portal.api_route:portal/src/app/api/health"])
        self.assertNotIn("data_clients", by_id["portal.api_route:portal/src/app/api/health"])
        self.assertEqual(by_id["portal.api_route:portal/src/app/api/query"]["data_clients"],
                         ["portal/src/lib/db.ts"])
        self.assertNotIn("models_via_shared", orders)
        self.assertEqual(by_id["portal.prisma_model:Widget"]["table"], "app_widget")
        self.assertEqual(by_id["portal-reporting-api.relation:public.app_sales_order"]["prisma_model"],
                         "SalesOrder")
        self.assertIn("portal-reporting-api.relation:sage.oe_order_header", by_id)
        for entity_set in ("salesOrders", "salesOrderLines", "vendors"):
            self.assertIn(f"business-central.entity_set:{entity_set}", by_id)
        methods = {e["name"]: e["methods"] for e in registry["entries"]
                   if e["system"] == "microsoft-graph"}
        self.assertEqual(methods, {
            "/drives/{}/items/{}": ["GET"], "/drives/{}/root:/{}:/content": ["PUT"],
            "/drives/{}/root:/{}:/children": ["POST"], "/drives/{}/root/children": ["POST"],
            "/drives/{}/items/{}/children": ["GET"],   # the wrapper's default method
            "/drives/{}/items/{}/listItem/fields": ["PATCH"],
            "/drives/{}/items/{}/thumbnails": [],      # only a comment names it
            # graphFetch is defined in another file: never guessed to be a read.
            "/users/{}/messages": ["unknown"], "/users/{}/sendMail": ["unknown"]})
        self.assertIn("sharepoint.site:PLXCUSTOMERS", by_id)
        self.assertEqual(by_id["plx-mc.mcp_tool:mc_get_task"]["evidence"],
                         [f"{PORTAL}:portal/src/lib/mcp/server.ts@{self.commit[:12]}:2"])
        self.assertNotIn("plx-mc.mcp_tool:mc_test_only", by_id)
        self.assertEqual(by_id["external.env_group:RESEND"]["variables"], ["RESEND_API_KEY"])
        self.assertIn("external.env_group:DOCUSIGN", by_id)
        self.assertIn("external.sdk:@aws-sdk/client-s3", by_id)
        self.assertNotIn("external.sdk:react", by_id)
        self.assertEqual(registry["upstream"], {PORTAL: {"commit": self.commit, "branch": "staging"}})
        for leaked in ("re_live_secret_value", "someone@petrasoap.com"):
            self.assertNotIn(leaked, text)


    def _v2_catalog(self) -> None:
        """v2 in the pinned catalogs: a manifest relation and an ``@@schema`` model."""
        manifest = json.loads(FILES["services/workbench-api/manifest/allowlist.json"])
        manifest["entries"].append({"schema": "v2", "relation": "batch_record", "domain": "fm",
                                    "columns": ["id"]})
        (self.up / "services/workbench-api/manifest/allowlist.json").write_text(
            json.dumps(manifest, indent=1))
        (self.up / "portal/prisma/schema.prisma").write_text(
            FILES["portal/prisma/schema.prisma"]
            + 'model Formula {\n  id String @id\n  @@map("formulation")\n  @@schema("v2")\n}\n')

    def _commit(self, message: str) -> str:
        _git(self.up, "add", ".")
        _git(self.up, "commit", "-qm", message)
        commit = _git(self.up, "rev-parse", "HEAD")
        self.pin(commit)
        return commit

    def test_catalog_entries_cite_their_relation_and_schema(self):
        self._v2_catalog()
        commit = self._commit("v2 in the catalogs")
        by_id = {e["id"]: e for e in self.build()["entries"]}
        self.assertEqual(by_id["portal-reporting-api.relation:v2.batch_record"]["evidence"], [
            f"{PORTAL}:services/workbench-api/manifest/allowlist.json@{commit[:12]}:21"])
        self.assertEqual(by_id["portal.prisma_model:Formula"]["schema"], "v2")

    def test_a_search_path_table_resolves_only_from_its_databases_catalogs_first_schema(self):
        self._v2_catalog()
        query = ' = (c, sql) => c.query("SET search_path TO v2, public").then(() => c.query(sql));\n'
        clients = {
            # The same search path on the Portal database the catalogs describe.
            "portal-db": 'const URL = "postgresql://u@h:5432/plxportal";\n',
            # An overridable URL whose default is that database: which database it connects to
            # is not pinned.
            "env-db": 'const URL = process.env.REPORTING_DATABASE_URL ?? "postgresql://u@h:5432/plxportal";\n',
            # Another default, but the runbook names its variable with the Portal database alone.
            "api-db": 'const URL = process.env.WB_API_DATABASE_URL ?? "postgresql://u@h:5432/other";\n',
            # fm-db's twin, sorting first: no route reaches it, so no route cites it.
            "a-fm-db": FILES["portal/src/lib/fm-db.ts"]}
        for name, text in clients.items():
            (self.up / f"portal/src/lib/{name}.ts").write_text(
                text if name == "a-fm-db" else f"{text}export const run{query}")
        sql = ('"SELECT id FROM batch_record JOIN formulation f ON true '
               'JOIN app_sales_order s ON true JOIN purchase_order p ON true"')
        for route, client, call in (("portal/batch", "portal-db", "run(c, "),
                                    ("env/batch", "env-db", "run(c, "),
                                    ("wb/batch", "api-db", "run(c, "),
                                    ("fm/batch", "fm", "queryFM(")):
            (self.up / f"portal/src/app/api/{route}/route.ts").parent.mkdir(parents=True)
            (self.up / f"portal/src/app/api/{route}/route.ts").write_text(
                f'import {{ {call.split("(")[0]} }} from "@/lib/{client}";\n'
                f"export const GET = (c) => {call}{sql});\n")
        commit = self._commit("clients of the v2 catalogs")
        by_id = {e["id"]: e for e in self.build()["entries"]}
        cite = f"{PORTAL}:services/workbench-api/manifest/allowlist.json@{commit[:12]}"
        runbook = f"{PORTAL}:docs/runbooks/WORKBENCH-READONLY-DB-ACCESS.md@{commit[:12]}"
        lib = f"{PORTAL}:portal/src/lib/%s.ts@{commit[:12]}:%d"
        resolved = {
            "batch_record": {"relation": "v2.batch_record", "evidence": f"{cite}:21"},
            "formulation": {"relation": "v2.formulation",
                            "evidence": f"{PORTAL}:portal/prisma/schema.prisma@{commit[:12]}:13"}}
        for route, database in (("portal/batch", {"name": "plxportal", "evidence": [
                                    lib % ("portal-db", 1), lib % ("portal-db", 2)]}),
                                ("wb/batch", {"name": "plxportal", "default": "other",
                                              "override": ["WB_API_DATABASE_URL"], "evidence": [
                                    lib % ("api-db", 1), lib % ("api-db", 2), f"{runbook}:5"]})):
            entry = by_id[f"portal.api_route:portal/src/app/api/{route}"]
            # The first path schema holds batch_record (the manifest) and formulation (an
            # @@schema model): each resolves, citing the catalog. The one v2 relation the
            # allowlist lists does not make it complete for v2: app_sales_order, which only
            # public lists, may be a v2 table too, so it stays unresolved, as purchase_order.
            self.assertEqual((entry["models"], entry["relations"], entry["search_path"],
                              entry["search_path_relations"], entry["search_path_resolved"]),
                             (["Formula"], ["v2.batch_record"], "v2, public",
                              ["app_sales_order", "purchase_order"], resolved), route)
            # The catalog database is the one of the runbook's address line (4), not the
            # first its prose names (command_center, 2).
            self.assertEqual(entry["search_path_database"], dict(database, catalog=f"{runbook}:4"))
        # The same SQL and path where no catalog is known to describe the database (an
        # overridable default, filemaker_replica): nothing binds to the Portal relation or
        # model of the same name, and each route cites the client module it reaches.
        tables = ["app_sales_order", "batch_record", "formulation", "purchase_order"]
        facts = sr.RepoFacts()
        for route, database in (
                ("env/batch", {"default": "plxportal", "override": ["REPORTING_DATABASE_URL"],
                               "evidence": [lib % ("env-db", 1), lib % ("env-db", 2)]}),
                ("fm/batch", {"default": "filemaker_replica", "override": ["FM_DATABASE_NAME"],
                              "evidence": [lib % ("fm-db", n) for n in (2, 3, 6)]})):
            entry = by_id[f"portal.api_route:portal/src/app/api/{route}"]
            self.assertEqual((entry["models"], entry["relations"], entry["search_path_relations"],
                              entry["search_path_database"]), ([], [], tables, database), route)
            self.assertNotIn("search_path_resolved", entry)
            self.assertEqual(sr.entry_status(entry, facts, None, {})["reason"],
                             "not_resolvable")
        # Two address lines naming different databases name none: nothing resolves.
        runbook_path = self.up / "docs/runbooks/WORKBENCH-READONLY-DB-ACCESS.md"
        runbook_path.write_text(runbook_path.read_text()
                                + "`primary.example.test`, port 5432, database `command_center`.\n")
        self._commit("a second address")
        portal = {e["id"]: e for e in self.build()["entries"]}[
            "portal.api_route:portal/src/app/api/portal/batch"]
        self.assertEqual((portal["search_path_relations"], "catalog" in portal["search_path_database"]),
                         (tables, False))

    def test_two_search_paths_reaching_one_module_are_refused(self):
        (self.up / "portal/src/lib/old-db.ts").write_text(
            'export const old = (c) => c.query("SET search_path TO legacy");\n')
        (self.up / "portal/src/lib/fm/index.ts").write_text(
            'export * from "../fm-db";\nexport * from "../old-db";\n')
        _git(self.up, "add", ".")
        _git(self.up, "commit", "-qm", "second path")
        self.pin(_git(self.up, "rev-parse", "HEAD"))
        with self.assertRaisesRegex(sr.RegistryError, "different search paths"):
            self.build()


class CheckTests(UpstreamFixture):
    def test_check_fails_until_regenerated_after_a_pin_moves_or_a_hand_edit(self):
        path = self.tmp / "source_registry.json"
        path.write_text(sr.render(self.build()))
        report = sr.check(path, self.pins, self.overrides)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["reproduced"], {"checked": True, "same": True})
        skipped = sr.check(path, self.pins, {PORTAL: str(self.tmp / "absent")})
        self.assertTrue(skipped["ok"])
        self.assertFalse(skipped["reproduced"]["checked"])

        (self.up / "portal/src/app/api/new/route.ts").parent.mkdir(parents=True)
        (self.up / "portal/src/app/api/new/route.ts").write_text("export function GET() {}\n")
        _git(self.up, "add", ".")
        _git(self.up, "commit", "-qm", "new route")
        moved = _git(self.up, "rev-parse", "HEAD")
        self.pin(moved)
        report = sr.check(path, self.pins, self.overrides)
        self.assertFalse(report["ok"])
        self.assertEqual(report["stale_pins"], [{"repo": PORTAL, "pinned": moved, "registry": self.commit}])
        path.write_text(sr.render(self.build()))
        self.assertTrue(sr.check(path, self.pins, self.overrides)["ok"])

        edited = json.loads(path.read_text())
        edited["entries"] = [e for e in edited["entries"] if e["kind"] != "mcp_tool"]
        path.write_text(sr.render(edited))
        report = sr.check(path, self.pins, {PORTAL: str(self.tmp / "absent")})
        self.assertFalse(report["ok"])
        self.assertIn("edited without regeneration", report["problems"][0])

    def test_committed_registry_matches_the_repository_pins(self):
        pins = json.loads(sr.PINS_PATH.read_text())
        absent = {repo: "/nonexistent" for repo in sr.repo_pins(pins)}
        report = sr.check(overrides=absent)
        self.assertTrue(report["ok"], report)
        with contextlib.redirect_stdout(io.StringIO()):   # generate --check checks, never writes (absent checkouts: generate would exit 2)
            self.assertEqual(sr.main(["generate", "--check"] + [
                arg for repo in absent for arg in ("--checkout", f"{repo}=/nonexistent")]), 0)
        by_id = {e["id"]: e for e in sr.load_registry()["entries"]}
        # plx-customer-portal@8ba6803 (TASK-2408) removed the PATCH.
        self.assertEqual(by_id["portal.api_route:portal/src/app/api/mrp/purchase-orders/"
                               "sourcing-requests"]["methods"], ["GET"])
        # queryFM sets search_path TO v2, public on filemaker_replica by default, which
        # FM_DATABASE_NAME or FM_DATABASE_URL overrides (fm-db.ts@2cb3760:5,9,15,28): no pinned
        # catalog describes the database it connects to, so its unqualified tables are not
        # guessed to be the Portal reporting relations of the same name.
        cust = by_id["portal.api_route:portal/src/app/api/fm/cust"]
        database = cust["search_path_database"]
        self.assertEqual((cust["relations"], cust["search_path"], cust["search_path_relations"],
                          database.get("name"), database.get("default"), database.get("override"),
                          "catalog" in database),
                         ([], "v2, public", ["customer", "customer_certification"], None,
                          "filemaker_replica", ["FM_DATABASE_NAME", "FM_DATABASE_URL"], False))
        self.assertRegex(by_id["portal-reporting-api.relation:public.app_sales_order"]["evidence"][0],
                         r"allowlist\.json@[0-9a-f]{12}:\d+$")

    def test_every_relation_the_pinned_manifest_serves_is_bound_and_no_other(self):
        """Portal TASK-3355, TASK-3390 and TASK-3525 (manifest/allowlist.json, 93 -> 102 -> 103 -> 104):
        each relation the manifest at the repo pin serves (its committed catalog snapshot, built
        from that manifest at that commit) is an entry and reaches ``bound`` through its generated
        raw type; a relation it does not serve is neither an entry nor bound, so
        sage.ap_vendor_remit is bound exactly when the pinned manifest lists it (TASK-3355 dropped
        it, TASK-3390 lifted it); columns the manifest excludes are bound by no property. TASK-3525
        lifts public.app_bc_entity_map alone (13 pinned columns, every other app_bc_*/bc_* stays
        denied): a route reading it resolves it. The two BC invoice snapshots are added from the live catalog (2026-10-26) and cite it."""
        pins = json.loads(sr.PINS_PATH.read_text())
        commit = sr.repo_pins(pins)[PORTAL]["commit"]
        catalog = json.loads((sr.ROOT / "ontology/schema/generated/meta/portal_catalog.json").read_text())
        self.assertEqual(catalog["source"]["repo"], PORTAL)
        self.assertTrue(commit.startswith(catalog["source"]["commit"]), (commit, catalog["source"]))
        self.assertIn(sr.MANIFEST_PATH, catalog["source"]["paths"])
        served = {r["name"] for r in catalog["relations"]}
        # Relations the live GET /v1/relations serves (248 on 2026-10-26) but the pinned manifest
        # does not list: added by `gen_ontology_skeleton merge-live`, never by hand. Each cites
        # the live read, not an allowlist line (the two BC invoice snapshots among them).
        LIVE_ADDED = set(catalog["live_added"]["relations"])
        self.assertEqual(len(LIVE_ADDED), 144)
        self.assertEqual(len(served), 104 + len(LIVE_ADDED))
        self.assertLessEqual({"public.bc_invoice_snapshot",
                              "public.bc_purchase_invoice_snapshot"}, LIVE_ADDED)
        entries = {e["id"]: e for e in sr.load_registry()["entries"]}
        tables = {e["name"]: e["table"] for e in entries.values() if e["kind"] == "prisma_model"}
        facts = sr.RepoFacts()
        listed = {e["name"] for e in entries.values()
                  if e["system"] == "portal-reporting-api" and e["kind"] == "relation"}
        self.assertEqual(listed, served)
        self.assertEqual({name for system, name in facts.bindings if system == "portal_api"}, served)
        new = ("public.app_approval_record", "public.app_supplier_product_tier",
               "sage.ap_open_document", "sage.ap_term", "sage.ap_vendor", "sage.gl_account",
               "sage.gl_batch", "sage.gl_je_detail", "sage.gl_je_header", "sage.ap_vendor_remit")
        for name in sorted(served):
            with self.subTest(relation=name):
                entry = entries[f"portal-reporting-api.relation:{name}"]
                if name in LIVE_ADDED:
                    self.assertIn("live GET /v1/relations", entry["evidence"][0])
                else:
                    self.assertRegex(entry["evidence"][0], rf"allowlist\.json@{commit[:12]}:\d+$")
                self.assertIn(("portal_api", name), facts.bindings)
                row = sr.entry_status(entry, facts, None, tables)
                self.assertGreater(sr.STAGES.index(row["status"]), sr.STAGES.index("bound"), row)
        for name in new:
            with self.subTest(new=name):
                self.assertEqual(name in served, ("portal_api", name) in facts.bindings)
        self.assertIn("sage.ap_vendor_remit", served)   # TASK-3390 at the pin
        remit = next(r for r in catalog["relations"] if r["name"] == "sage.ap_vendor_remit")
        self.assertIn("_sage_pk", remit["columns"])
        self.assertFalse({"textstre1", "codepstl", "textphon1", "audtuser"} & set(remit["columns"]))
        generated = [t for path in sorted((sr.ROOT / "ontology/schema/generated").glob("portal_*.json"))
                     for t in json.loads(path.read_text())["object_types"]]
        def bound_columns(binding):
            return [p["name"] for t in generated for p in t.get("properties", ())
                    if any(s.get("binding") == binding for s in p.get("sources", ()))]
        vendor = bound_columns("gen.sage.ap_vendor")
        self.assertIn("vendorid", vendor)
        self.assertFalse({"taxnbr", "brn", "amtcrlimt"} & set(vendor), vendor)
        self.assertIn("idvendrmit", bound_columns("gen.sage.ap_vendor_remit"))
        entity_map = ["bc_company_id", "bc_environment", "bc_external_document_number", "bc_number",
                      "bc_system_id", "created_at", "entity_type", "id", "last_synced_at",
                      "source_id", "source_system", "status", "updated_at"]
        bridge = next(r for r in catalog["relations"] if r["name"] == "public.app_bc_entity_map")
        self.assertEqual(sorted(bridge["columns"]), entity_map)       # TASK-3525 at the pin
        self.assertEqual(sorted(bound_columns("gen.public.app_bc_entity_map")), entity_map)
        # At the pin only app_bc_entity_map of the app_bc_*/bc_* relations is served; any
        # other one is served because the live read lists it (live_added), never by hand.
        bc_served = {n for n in served if n.split(".", 1)[1].startswith(("app_bc_", "bc_"))}
        self.assertEqual(bc_served - LIVE_ADDED, {"public.app_bc_entity_map"})
        self.assertLessEqual({"public.bc_invoice_snapshot", "public.bc_purchase_invoice_snapshot"},
                             bc_served & LIVE_ADDED)
        readers = [e for e in entries.values() if e["kind"] == "api_route"
                   and "public.app_bc_entity_map" in e.get("relations", ())]
        self.assertGreaterEqual(len(readers), 4)
        for route in readers:
            self.assertNotIn("app_bc_entity_map", route.get("unresolved_relations", ()), route["id"])
        ownership = entries["portal.api_route:portal/src/app/api/mrp/orders/[id]/ownership"]
        self.assertIn("bc_invoice_snapshot", ownership.get("unresolved_relations", ()))


def _entry(system: str, kind: str, name: str, **attrs) -> dict:
    return dict({"id": f"{system}.{kind}:{name}", "system": system, "kind": kind, "name": name,
                 "evidence": [f"{PORTAL}:x@000000000000"]}, **attrs)


class CoverageTests(unittest.TestCase):
    def test_stages_come_from_repo_code_and_the_newest_live_result(self):
        entries = [
            _entry("business-central", "entity_set", "purchaseOrders"),
            _entry("business-central", "entity_set", "salesQuotes"),
            _entry("plx-mc", "mcp_tool", "mc_unbound_probe"),   # no binding or scope names it
            _entry("portal", "prisma_model", "SalesOrder", table="app_sales_order"),
            _entry("portal", "prisma_model", "Widget", table="app_widget"),
            _entry("portal", "api_route", "/api/x", models=["SalesOrder", "Widget"]),
            _entry("portal", "prisma_model", "AuditLog", table="app_audit_log"),
            _entry("portal", "api_route", "/api/me", models=["SalesOrder", "AuditLog"]),
            _entry("sharepoint", "site", "PLXCUSTOMERS"),
            _entry("sharepoint", "site", "Maintenance"),
            _entry("portal", "api_route", "/api/auth-only", models_via_shared=["User"]),
            _entry("portal", "api_route", "/api/fm/po", models_via_shared=["User"],
                   data_clients=["portal/src/lib/fm-db.ts"], unresolved_relations=["purchase_order"]),
            _entry("portal", "api_route", "/api/fm/cust", models_via_shared=["User"],
                   data_clients=["portal/src/lib/fm-db.ts"], search_path="v2, public",
                   search_path_relations=["customer", "customer_certification"]),
            _entry("portal", "api_route", "/api/so-pathed", models=["SalesOrder"],
                   search_path="v2, public", search_path_relations=["customer"],
                   search_path_database={"name": "plxportal", "catalog": "runbook"}),
            _entry("portal", "api_route", "/api/so-raw", models=["SalesOrder"],
                   unresolved_relations=["customer_certification"]),
            _entry("portal", "api_route", "/api/health"),
            _entry("portal", "api_route", "/api/query", data_clients=["portal/src/lib/db.ts"]),
            _entry("portal", "api_route", "/api/ghost", models=["Ghost"]),   # no known table
            _entry("external", "host", "api.github.com"),          # scoped by entry id
            _entry("infra-ops", "mcp_tool", "break_glass"),       # scoped by system.kind
        ]
        registry = {"entries": entries, "entries_digest": sr.entries_digest(entries),
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        older = {"_from": "daily", "generated_at": "2026-10-04T00:00:00Z", "checks": [
            {"id": "bc.x", "source": "business-central", "result": "fail",
             "detail": {"fields": {"units[*].entity_set": ["purchaseOrders"]}}}]}
        newer = {"_from": "hourly", "generated_at": "2026-10-05T00:00:00Z", "checks": [
            {"id": "bc.x", "source": "business-central", "result": "pass",
             "detail": {"fields": {"units[*].entity_set": ["purchaseOrders"]}}},
            {"id": "mirror.x", "source": "portal-workbench-api", "result": "blocked",
             "detail": {"fields": {"relations": ["app_sales_order"]}}}]}
        facts = sr.RepoFacts()
        doc = sr.gaps(registry, [newer, older], facts)   # the newer run of a check wins, in any order
        rows = {row["id"]: row for row in doc["uncovered"]}
        self.assertNotIn("business-central.entity_set:purchaseOrders", rows)  # evaluated
        self.assertEqual(doc["counts"]["by_status"]["evaluated"], 1)

        def stage(name):
            row = rows[name]
            return row["status"], row["missing"], row["reason"]
        # A generated BC set reaches fixture_queryable; no live check names it.
        self.assertEqual(stage("business-central.entity_set:salesQuotes"),
                         ("fixture_queryable", "evaluated", "no_live_check"))
        self.assertEqual(stage("plx-mc.mcp_tool:mc_unbound_probe"), ("declared", "bound", "no_source_family"))
        self.assertEqual(stage("portal.prisma_model:SalesOrder"),
                         ("fixture_queryable", "evaluated", "live_not_passing"))
        self.assertEqual(stage("portal.api_route:/api/x"), ("declared", "bound", "no_binding"))
        self.assertEqual(rows["portal.api_route:/api/x"]["blocking"], ["portal_api:public.app_widget"])
        # A model the API does not serve is declared by a not_served type with an ask (the
        # generator's coverage map); a route is credited through its models.
        self.assertEqual(stage("portal.prisma_model:AuditLog"), ("declared", "bound", "not_served"))
        self.assertEqual(stage("portal.api_route:/api/me"), ("declared", "bound", "not_served"))
        self.assertEqual(rows["portal.api_route:/api/me"]["blocking"], ["portal_api:public.app_audit_log"])
        self.assertEqual(stage("sharepoint.site:PLXCUSTOMERS"),
                         ("fixture_queryable", "evaluated", "no_live_check"))
        # No capture target names it, but the estate's discover rule captures every site
        # it does not exclude.
        self.assertEqual(stage("sharepoint.site:Maintenance"),
                         ("fixture_queryable", "evaluated", "no_live_check"))
        # Data the route's own code reaches through a client of its own, named or not.
        self.assertEqual(stage("portal.api_route:/api/fm/po"),
                         ("declared", "bound", "reaches_data_client"))
        self.assertEqual(rows["portal.api_route:/api/fm/po"]["unresolved_relations"],
                         ["purchase_order"])
        # A table read through a client with its own search path: which schema holds it is
        # not known, ahead of the client itself, and bound data does not carry a route past it.
        # A client of a database no pinned catalog describes (none named, here): the table is
        # not resolvable from pinned evidence.
        self.assertEqual(stage("portal.api_route:/api/fm/cust"),
                         ("declared", "bound", "not_resolvable"))
        # The pinned FM routes (gap-480fe9cd6434): filemaker_replica has no pinned catalog.
        real = sr.load_registry()["entries"]
        tables = {e["name"]: e["table"] for e in real if e["kind"] == "prisma_model"}
        fm = [sr.entry_status(e, facts, None, tables) for e in real
              if e["kind"] == "api_route" and e["name"].startswith("/api/fm/")
              and e.get("search_path_relations")]
        self.assertEqual((len(fm), {(r["status"], r["missing"], r["reason"]) for r in fm}),
                         (10, {("declared", "bound", "not_resolvable")}))
        self.assertEqual((rows["portal.api_route:/api/fm/cust"]["search_path"],
                          rows["portal.api_route:/api/fm/cust"]["search_path_relations"]),
                         ("v2, public", ["customer", "customer_certification"]))
        self.assertEqual(stage("portal.api_route:/api/so-pathed"),
                         ("declared", "bound", "search_path_unresolved"))
        # Its bound data does not carry a route past a table nothing resolves.
        self.assertEqual(stage("portal.api_route:/api/so-raw"),
                         ("declared", "bound", "unresolved_relation"))
        self.assertEqual(stage("portal.api_route:/api/ghost"), ("declared", "bound", "no_binding"))
        # A route reaching a data client may read data no literal names: a gap, never terminal.
        self.assertEqual(stage("portal.api_route:/api/query"),
                         ("declared", "bound", "reaches_data_client"))
        # Terminal entries are no gap: a route reaching no data, decided out-of-scope entries.
        # A route whose only data is the shared auth layer's is no source of its own.
        for terminal in ("portal.api_route:/api/health", "external.host:api.github.com",
                         "infra-ops.mcp_tool:break_glass", "portal.api_route:/api/auth-only"):
            self.assertNotIn(terminal, rows)
        self.assertEqual((doc["counts"]["by_status"]["not_a_data_source"],
                          doc["counts"]["by_status"]["out_of_scope"],
                          doc["counts"]["by_status"]["auth_layer_only"]), (1, 2, 1))
        scopes = {s["scope"]: s for s in doc["out_of_scope"]}
        self.assertEqual(scopes["external.host:api.github.com"],
                         {"scope": "external.host:api.github.com", "entries": 1,
                          "decision": "devloop/DECISIONS/2026-10-07-source-scope-out-of-scope.md"})
        self.assertEqual(scopes["infra-ops.mcp_tool"]["entries"], 1)
        self.assertEqual(scopes["external.host"]["entries"], 0)   # the entry id scope wins
        self.assertEqual({k for row in doc["uncovered"] for k in row} - {
            "blocking", "unresolved_relations", "search_path", "search_path_relations",
            "search_path_database"},
                         {"id", "system", "kind", "name", "status", "missing", "reason", "evidence"})
        self.assertIn("bc.general_ledger_entries", {b["binding"] for b in doc["bound_without_entry"]})
        self.assertEqual([s["from"] for s in doc["live"]], ["hourly", "daily"])
        no_live = {row["id"]: row for row in sr.gaps(registry, None, facts)["uncovered"]}
        self.assertEqual(no_live["business-central.entity_set:purchaseOrders"]["reason"], "no_live_results")
        # Without its decided record an auth-only route stays a gap.
        facts.auth_layer_decided = False
        undecided = {row["id"]: row for row in sr.gaps(registry, None, facts)["uncovered"]}
        self.assertEqual(undecided["portal.api_route:/api/auth-only"]["reason"], "models_via_shared")
        facts.auth_layer_decided = True

        # Synthetic rows of every binding came back through plx.find/plx.object (the real
        # schema, projector and tools); the denominator is the entries of schema kinds.
        self.assertEqual({family: (block["fixture_queryable"], block["not_queryable"])
                          for family, block in doc["fixture"]["by_family"].items()},
                         {family: (block["bindings"], {})
                          for family, block in doc["fixture"]["by_family"].items()})
        self.assertLessEqual({"portal_api", "bc", "graph_mail", "sharepoint"},
                             set(doc["fixture"]["by_family"]))
        coverage = doc["counts"]["coverage"]
        # The MCP tool is inventory, not data; a route that is not a data source is not counted.
        self.assertEqual(coverage["entries"], 15)
        self.assertEqual(coverage["by_family"]["bc"],
                         {"entries": 2, "reached": {"declared": 2, "bound": 2, "captured": 2,
                                                    "queryable": 2, "fixture_queryable": 2,
                                                    "evaluated": 1}})
        # A binding whose synthetic rows do not come back stops its entries there, even
        # with a passing live check.
        bid = facts.bindings[("bc", "purchaseOrders")][0]
        facts.fixture = dict(facts.fixture, **{bid: "not_found"})
        rows = {row["id"]: row for row in sr.gaps(registry, [newer], facts)["uncovered"]}
        self.assertEqual(stage("business-central.entity_set:purchaseOrders"),
                         ("queryable", "fixture_queryable", "fixture_not_queryable"))

    def test_a_check_declaring_registry_names_credits_each_name_by_its_outcome(self):
        """``schema.portal_queryable`` declares ``registry_names``: a name is credited only
        under ``queryable`` of a passing check; ``read_empty`` is unexercised, never
        queryable; ``not_queryable`` fails only the relations it names; a check without the
        declaration credits every name with its result."""
        entries = [_entry("portal", "prisma_model", "SalesOrder", table="app_sales_order")]
        registry = {"entries": entries, "entries_digest": sr.entries_digest(entries),
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        facts = sr.RepoFacts()
        name = {"public_app": ["app_sales_order"]}
        for check, result, fields, expected in (
                ("schema.portal_queryable", "pass", {"queryable": name}, None),
                ("schema.portal_queryable", "fail", {"queryable": name}, "live_not_passing"),
                ("schema.portal_queryable", "pass", {"read_empty": name}, "live_unexercised"),
                # A bound relation the API reports unavailable (relation_unavailable) is named,
                # and fails: no live check aligned with it is never the reason.
                ("schema.portal_queryable", "pass",
                 {"unavailable": name},
                 "live_not_passing"),
                ("schema.portal_queryable", "pass",
                 {"queryable": {"public_app": ["app_quote"]},
                  "not_queryable": {"public_app": {"app_sales_order": "read_TimeoutError"}}},
                 "live_not_passing"),
                ("schema.portal_queryable", "pass", {"bound_relation_count": 93,
                                                     "relations": name}, "no_live_check"),
                ("mirror.x", "pass", {"read_empty": name}, None)):
            with self.subTest(check=check, result=result, fields=fields):
                live = [{"_from": "daily", "generated_at": "2026-10-07T00:00:00Z", "checks": [
                    {"id": check, "source": "portal-workbench-api", "result": result,
                     "detail": {"fields": fields}}]}]
                row = sr.coverage(registry, live, facts)[0]
                self.assertEqual(row.get("reason"), expected, row)
                self.assertEqual(row["status"],
                                 "evaluated" if expected is None else "fixture_queryable")

    def test_a_not_served_mapping_counts_only_for_a_not_served_type_with_an_ask(self):
        """Fail closed: coverage naming a served type, an unknown type or a not_served type
        without an upstream ask declares nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in sr.FACT_INPUTS:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (shutil.copytree if name.endswith("/") else shutil.copy)(
                    sr.ROOT / name, root / name)
            coverage_path = root / "ontology/schema/generated/meta/coverage.json"
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            served_tid = coverage["relations"]["public.app_customer"]["object_type"]
            coverage["not_served"] = {
                "public.app_audit_log": coverage["not_served"]["public.app_audit_log"],
                "public.app_widget": {"object_type": served_tid},
                "public.app_gizmo": {"object_type": "NoSuchType"},
                "public.app_gadget": {"object_type": "RelPublicAppGadget"}}
            coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
            unasked = root / "ontology/schema/generated/portal_not_served_a_e.json"
            doc = json.loads(unasked.read_text(encoding="utf-8"))
            doc["object_types"].append({
                "id": "RelPublicAppGadget", "description": "x", "bindings": [],
                "properties": [], "availability": {"state": "not_served", "reason": "x"},
                "evidence": doc["object_types"][0]["evidence"]})
            unasked.write_text(json.dumps(doc), encoding="utf-8")
            bc_path = root / "ontology/schema/generated/meta/bc_coverage.json"
            bc_coverage = json.loads(bc_path.read_text(encoding="utf-8"))
            bc_coverage["not_served"]["locations"] = {"object_type": "RelBcLocations"}  # served
            bc_path.write_text(json.dumps(bc_coverage), encoding="utf-8")
            facts = sr.RepoFacts(root)
        self.assertEqual(set(facts.unserved), {("portal_api", "public.app_audit_log"),
                                               ("bc", "postedSalesInvoices"),
                                               ("bc", "salesReceivablesSetup"),
                                               ("bc", "plxCustomerShipTos"),
                                               ("bc", "pdfDocumentContent"),
                                               # Portal ca27b78a TASK-2913 (at the 0323e19c pin):
                                               # read-only bridge pages, not served yet
                                               ("bc", "plxPostedSalesInvoices"),
                                               ("bc", "plxPostedSalesInvoiceLines"),
                                               ("bc", "plxSalesReceivablesSetups")})


class LiveAlignmentTests(unittest.TestCase):
    """A live check credits only what it read: an MCP tool through the relation its binding
    family holds (a check aligned with the tool's system is aligned with that family), a
    Graph template only through a reader operation the check's run was answered for."""

    @classmethod
    def setUpClass(cls):
        cls.facts = sr.RepoFacts()
        cls.entries = {e["id"]: e for e in sr.load_registry()["entries"]}

    def coverage(self, ids, checks):
        entries = [self.entries[i] for i in ids]
        registry = {"entries": entries, "entries_digest": sr.entries_digest(entries),
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        live = [{"_from": "hourly", "generated_at": "2026-10-08T00:00:00Z", "checks": [
            {"id": cid, "source": source, "result": result, "detail": {"fields": fields}}
            for cid, source, result, fields in checks]}]
        doc = sr.gaps(registry, live, self.facts)
        uncovered = {row["id"] for row in doc["uncovered"]}
        rows = {row["name"]: (row["status"], row.get("reason"))
                for row in sr.coverage(registry, live, self.facts)}
        # gaps() lists exactly the entries coverage leaves short of evaluated.
        self.assertEqual({self.entries[i]["name"] for i in uncovered},
                         {n for n, (status, _) in rows.items() if status != "evaluated"})
        return rows

    def test_an_mcp_tool_is_credited_only_by_a_run_that_called_it(self):
        """A check credits a tool when it read the tool's bound data and its run's
        ``tools_called`` lists the tool; a read of that data through another tool is
        recorded as ``bound_data_read`` and credits nothing."""
        mc = ["mc_get_context", "mc_search_tasks", "mc_list_buckets", "mc_search_knowledge",
              "mc_get_task"]
        brain = ["brain_get_node", "brain_timeline", "brain_search"]
        ids = [f"plx-mc.mcp_tool:{t}" for t in mc] + [f"plx-brain.mcp_tool:{t}" for t in brain]
        units = "passes[*].units[*].collection"
        read = ["tasks", "buckets", "knowledge", "tasks", "buckets", "knowledge"]
        capture = ["mc_get_context", "mc_list_buckets", "mc_search_knowledge"]

        def run(result, collections=read, called=capture, mc_source="mission-control-mcp"):
            return self.coverage(ids, [
                ("mc.sources_capture", mc_source, result,
                 {units: collections, "tools_called": called,
                  "passes[*].units[*].reason": ["approvals"]}),   # not a registry_names path
                # Reads brain items over HTTP: it records no tools_called.
                ("knowledge.brain_source", "vmc", result, {"relations": {"items": 3}})])
        rows = run("pass")
        self.assertEqual({n for n, (s, _) in rows.items() if s == "evaluated"}, set(capture))
        # MC tasks were read through mc_get_context, and brain items without any MCP tool.
        unused = ["mc_search_tasks"] + brain
        self.assertEqual({n: rows[n] for n in unused},
                         dict.fromkeys(unused, ("fixture_queryable", "bound_data_read")))
        # No unit read approvals or task events: the tool reading them has no check.
        self.assertEqual(rows["mc_get_task"], ("fixture_queryable", "no_live_check"))
        rows = run("fail")
        self.assertEqual({n: rows[n][1] for n in capture}, dict.fromkeys(capture, "live_not_passing"))
        self.assertEqual(rows["mc_search_tasks"], ("fixture_queryable", "bound_data_read"))
        # Approvals and task events read, but not through mc_get_task: still not credited.
        every = read + ["approvals", "task_events"]
        self.assertEqual(run("pass", every)["mc_get_task"], ("fixture_queryable", "bound_data_read"))
        rows = run("pass", every, capture + ["mc_get_task", "mc_search_tasks"])
        self.assertEqual({n: rows[n] for n in mc}, dict.fromkeys(mc, ("evaluated", None)))
        # A tools_called name of a check of another source credits nothing either.
        rows = run("pass", called=mc, mc_source="business-central")
        self.assertEqual({n: rows[n] for n in mc[:4]},
                         dict.fromkeys(mc[:4], ("fixture_queryable", "no_live_check")))
        # The uncovered row names the check that read the data.
        entries = [self.entries[i] for i in ids]
        registry = {"entries": entries, "entries_digest": sr.entries_digest(entries),
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        live = [{"_from": "hourly", "generated_at": "2026-10-08T00:00:00Z", "checks": [
            {"id": "mc.sources_capture", "source": "mission-control-mcp", "result": "pass",
             "detail": {"fields": {units: read, "tools_called": capture}}},
            {"id": "knowledge.brain_source", "source": "vmc", "result": "pass",
             "detail": {"fields": {"relations": {"items": 3}}}}]}]
        rows = {r["name"]: r for r in sr.gaps(registry, live, self.facts)["uncovered"]}
        # "Through another tool" only when the run called one; else it names the direct read.
        self.assertEqual(rows["mc_search_tasks"]["bound_data_read"], [
            {"check": "mc.sources_capture", "result": "pass", "read": "through another tool: its "
             "run called mc_get_context, mc_list_buckets, mc_search_knowledge"}])
        self.assertEqual(rows["brain_get_node"]["bound_data_read"], [
            {"check": "knowledge.brain_source", "result": "pass",
             "read": "a direct read: its run called no MCP tool"}])

    def test_a_graph_template_is_credited_only_by_an_operation_its_check_read(self):
        """The reads the adapter tests record for each command (an injected transport):
        integrations/tests/test_capture_mail.py, test_capture_sharepoint.py and
        test_live_source_adapters.py (the probe)."""
        templates = ["/drives/{}/items/{}", "/drives/{}/items/{}/children", "/drives/{}/root",
                     "/users/{}/messages/{}", "/users/{}/messages",
                     # Read through other forms: a delta, a pre-authenticated download.
                     "/users/{}/mailFolders/{}/messages", "/drives/{}/items/{}/content",
                     # The relation it binds is captured, but no Workbench reader requests it
                     # (the Portal stopped calling the workbook templates at c09de5d).
                     "/drives/{}/root:/{}:/listItem/fields"]
        ids = [f"microsoft-graph.endpoint:{t}" for t in templates]
        mail = ["graph.folder_messages_delta", "graph.mail_folder", "graph.message",
                "graph.message_attachment", "graph.message_attachments", "graph.user"]
        probe = ["graph.drive_children", "graph.drive_item", "graph.drive_permissions",
                 "graph.drive_root", "graph.mail_folder", "graph.message", "graph.messages",
                 "graph.site", "graph.site_drives", "graph.user", "graph.users"]
        never = {t: ("fixture_queryable", "no_live_check") for t in templates[5:]}
        # A no_live_check template says what it waits for: a check's operations_read record
        # of a reader operation that requests it, or a reader operation to request it at all.
        registry = {"entries": [self.entries[i] for i in ids], "entries_digest": "",
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        waits = {r["name"]: r.get("waits_for") for r in sr.gaps(registry, [
            {"_from": "hourly", "checks": []}], self.facts)["uncovered"]}
        self.assertEqual(waits, dict(dict.fromkeys(templates[:7], "operations_read"),
                                     **{templates[7]: "reader_operation"}))

        def run(mail_result, probe_result="pass", probe_ops=probe):
            return self.coverage(ids, [
                ("graph.mail_capture", "microsoft-graph", mail_result,
                 {"operations_read": mail, "relations": {"messages": 3}}),
                ("graph.adapter_probe", "microsoft-graph", probe_result,
                 {"operations_read": probe_ops}),
                # A relation name credits no template (it once credited all 30 driveItems).
                ("capture.sharepoint", "sharepoint", "pass", {"relations": {"driveItems": 3}})])
        rows = run("pass")
        self.assertEqual(rows, dict(dict.fromkeys(templates[:5], ("evaluated", None)), **never))
        rows = run("fail", "fail")
        self.assertEqual(rows, dict(dict.fromkeys(templates[:5], (
            "fixture_queryable", "live_not_passing")), **never))
        # A result recorded before the checks named their reads credits nothing.
        rows = run("pass", "pass", [])
        self.assertEqual({t: rows[t] for t in templates[:3]},
                         dict.fromkeys(templates[:3], ("fixture_queryable", "no_live_check")))


class DeclaredTests(unittest.TestCase):
    """Repository inputs copied to a temporary root (``RepoFacts(root)``, as the gap ledger
    reads another commit), then edited."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for want in sr.FACT_INPUTS:
            src = sr.ROOT / want
            for path in (sorted(src.rglob("*")) if want.endswith("/") else [src]):
                if path.is_file():
                    target = self.root / path.relative_to(sr.ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())

    def test_bindings_and_action_types_declaring_an_entry_bind_it_whatever_its_kind(self):
        path = self.root / "ontology/schema/generated/portal_orders.json"
        doc = json.loads(path.read_text())
        binding = next(b for t in doc["object_types"] for b in t["bindings"]
                       if b["relation"] == "silver.order_entries")
        binding["registry_entries"] = ["plx-mc.mcp_tool:mc_get_task", "plx-mc.mcp_tool:mc_gone"]
        fmt = json.loads((sr.ROOT / "ontology/schema/_format.schema.json").read_text())
        self.assertEqual(json_contract.validate(doc, fmt, root=fmt), [])   # the gate's format
        path.write_text(json.dumps(doc))
        action = "PLXSalesBridge_EnsureSalesOrderManualNos"
        orders = self.root / "ontology/schema/domains/orders.json"
        domain = json.loads(orders.read_text())
        domain.setdefault("action_types", []).append({
            "id": "EnsureSalesOrderNos", "description": "A human runs the BC bridge action.",
            "target": "SalesOrder", "params": {"evidence": {
                "type": "citation", "provenance": ["cited_value"], "required": True}},
            "submission_criteria": [{"id": "cited", "description": "The evidence resolves."}],
            "owner_system": "bc", "executor": "human_in_owner_system",
            "approval": {"route": "mc_request_approval"}, "verification": "read_back",
            "idempotency": ["target"], "evidence": [{"kind": "workbench_code", "path": "x.py"}],
            "registry_entries": [f"business-central.web_service_action:{action}"]})
        self.assertEqual(json_contract.validate(domain, fmt, root=fmt), [])
        orders.write_text(json.dumps(domain))
        entries = [_entry("plx-mc", "mcp_tool", "mc_get_task"),
                   _entry("plx-mc", "mcp_tool", "mc_unbound_probe"),
                   _entry("business-central", "web_service_action", action)]
        registry = {"entries": entries, "entries_digest": sr.entries_digest(entries),
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        facts = sr.RepoFacts(self.root)
        doc = sr.gaps(registry, None, facts)
        rows = {row["id"]: row for row in doc["uncovered"]}
        self.assertEqual((rows["plx-mc.mcp_tool:mc_get_task"]["status"],
                          rows["plx-mc.mcp_tool:mc_get_task"]["reason"]),
                         ("fixture_queryable", "no_live_results"))
        self.assertEqual(rows["plx-mc.mcp_tool:mc_unbound_probe"]["reason"], "no_source_family")
        # An action is verified per call by read-back, never captured: terminal, not a gap.
        action_id = f"business-central.web_service_action:{action}"
        self.assertNotIn(action_id, rows)
        self.assertEqual({r["id"]: r["status"] for r in sr.coverage(registry, None, facts)}
                         [action_id], "bound_action")
        # A declared id the registry lacks is shown, never silently bound.
        self.assertIn({"binding": binding["id"], "source": "portal_api",
                       "relation": "silver.order_entries", "registry_entry": "plx-mc.mcp_tool:mc_gone"},
                      doc["bound_without_entry"])

    def test_a_site_the_estate_rule_excludes_is_out_of_scope_and_a_named_site_is_credited(self):
        entries = [_entry("sharepoint", "site", "PLXCUSTOMERS"),
                   _entry("sharepoint", "site", "Maintenance")]
        registry = {"entries": entries, "entries_digest": sr.entries_digest(entries),
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        live = [{"_from": "daily", "generated_at": "2026-10-07T00:00:00Z", "checks": [
            {"id": "capture.sharepoint_estate", "source": "sharepoint", "result": "pass",
             "detail": {"fields": {"registry_sites.captured": ["Maintenance"],
                                   "registry_sites.unexercised": ["PLXCUSTOMERS"]}}}]}]

        def stages():
            return {row["name"]: (row["status"], row.get("reason"))
                    for row in sr.coverage(registry, live, sr.RepoFacts(self.root))}
        self.assertEqual(stages(), {"Maintenance": ("evaluated", None),
                                    "PLXCUSTOMERS": ("fixture_queryable", "live_unexercised")})
        estate = self.root / "integrations/sharepoint_estate_scope.json"
        doc = json.loads(estate.read_text())
        doc["discover"]["exclude"].append({"site": "maintenance"})   # case-insensitive
        estate.write_text(json.dumps(doc))
        # Excluded: out of the capture scope whatever a check names; a target still counts.
        self.assertEqual(stages(), {"Maintenance": ("bound", "out_of_scope"),
                                    "PLXCUSTOMERS": ("fixture_queryable", "live_unexercised")})
        estate.unlink()   # an older ref without the estate scope: targets alone
        self.assertEqual(stages()["Maintenance"], ("bound", "out_of_scope"))
        doc["discover"]["exclude"] = [{"site": "bad name!"}]
        estate.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):   # malformed: refused, never read as no rule
            sr.RepoFacts(self.root)

    def test_a_graph_row_is_bound_classified_or_blocked_with_its_record(self):
        """The generated endpoint map's outcomes as gap rows: a folded template written out
        and bound (captured drive items), a template upstream only POSTs (list views) an
        upstream write, a bound relation a capture reads (list items) never blocked; a
        blocker on a relation a capture reads is refused (stale)."""
        e = "microsoft-graph.endpoint:"
        wanted = {e + t for t in ("/drives/{}/{}/{}", "/sites/{}/lists/{}/views",
                                  "/sites/{}/lists/{}/items")}
        entries = [x for x in sr.load_registry()["entries"] if x["id"] in wanted]
        registry = {"entries": entries, "entries_digest": sr.entries_digest(entries),
                    "upstream": {PORTAL: {"commit": "0" * 40}}}
        rows = {row["name"]: row for row in sr.gaps(registry, None, sr.RepoFacts(self.root))[
            "uncovered"]}
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows["/drives/{}/{}/{}"]["status"], rows["/drives/{}/{}/{}"]["reason"]),
                         ("fixture_queryable", "no_live_results"))
        views = rows["/sites/{}/lists/{}/views"]
        self.assertEqual((views["status"], views["reason"], views["not_business_data"]),
                         ("declared", "not_business_data", "write_operation"))
        self.assertTrue(sr.DECIDED.search((self.root / views["decision"]).read_text()))
        items = rows["/sites/{}/lists/{}/items"]
        self.assertEqual((items["status"], items["reason"], items.get("blocked")),
                         ("fixture_queryable", "no_live_results", None))
        decision = "devloop/DECISIONS/2026-10-07-ovs-teams-capture.md"
        self.assertTrue((self.root / decision).is_file())
        path = self.root / sr.GRAPH_ENDPOINTS
        doc = json.loads(path.read_text())
        doc["endpoints"][e + "/sites/{}/lists/{}/items"]["blocked"] = {
            "reason": "x", "decision": decision}
        path.write_text(json.dumps(doc))
        with self.assertRaisesRegex(sr.RegistryError, "declared blocked but a capture reads it"):
            sr.RepoFacts(self.root)

    def test_a_scope_without_a_decided_record_is_refused(self):
        scope = self.root / "ontology/source_scope.json"
        decided = "devloop/DECISIONS/2026-10-07-external-infra-ops-not-sources.md"
        self.assertIn("external.host", sr.load_scope(self.root))
        (self.root / "devloop/DECISIONS/open-probe.md").write_text("# Q\n\n- status: open\n")
        for decision, refusal in (("devloop/DECISIONS/2026-10-08-no-such-record.md", "missing"),
                                  ("devloop/DECISIONS/open-probe.md", "not status: decided"),
                                  ("docs/elsewhere.md", "not a devloop/DECISIONS")):
            scope.write_text(json.dumps({"format": sr.SCOPE_FORMAT, "out_of_scope": {
                "external.host": decided, "external.sdk": decision}}))
            with self.assertRaisesRegex(sr.RegistryError, refusal):
                sr.load_scope(self.root)
        scope.write_text(json.dumps({"format": sr.SCOPE_FORMAT, "out_of_scope": {
            "external host": decided}}))
        with self.assertRaisesRegex(sr.RegistryError, "neither"):
            sr.load_scope(self.root)


if __name__ == "__main__":
    unittest.main()
