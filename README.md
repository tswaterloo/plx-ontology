# PLX Agent Workbench

One schema-defined ontology projected from cited source captures (Portal reporting
API mirror, Graph mail, SharePoint originals, Business Central), read through generic,
permission-aware tools by an MCP client and by a read-only operator console. Project rules: `CLAUDE.md`.

## Entry points

- **Case host (agents):** `python3 -m operating.mcp_server --state-dir DIR
  --source-mirror-audience FILE --employee-policy POLICY --portal-access PORTAL_SNAPSHOT
  --session-ticket-env VAR` (a single-use signed start-up ticket; `--dev-configured-employee
  --employee-oid <GUID>` is development only and never reads a real source) speaks
  newline-delimited MCP JSON-RPC on stdio and serves only the case tools
  for one verified employee, author kind `agent`
  (`operating/session_identity.py`). There is no default principal; `--fixture
  --principal staff:operations` is a demo identity, refused beside any real source.
- **Operator console (people):** `python3 -m operator_api.workspace.case_console --state-dir DIR
  --source-mirror-audience FILE --employee-policy POLICY --portal-access PORTAL_SNAPSHOT`
  serves the read-only case workspace on a loopback port; every request authenticates
  with the employee's signed session ticket (`#access=<ticket>`, author kind `human`).
  Fixture cases: `python3 -m operator_api.tests.case_fixtures --state-dir NEW_DIR --fixture
  --seed-fixtures --principal staff:operations` (the same console with the test fixture world; the printed
  `#access=...` token is local, not PLX identity).
- **Ingestion:** `python3 -m operating.host run|serve|check --state-dir DIR --config HOST.json`
  runs capture cycles (Portal mirror, BC, mail, SharePoint) into one persistent state directory
  and rebuilds the projection from them, through the GET-only clients; every live behaviour has a spec in
  the live-check specs and is labelled live-unverified until a verifier run passes it.

## Retired

The `UnifiedPlatform` product service and everything only it reached (objectives,
campaigns, business watches, autonomy worker, transaction workspace, named Portal
streams and the Portal bridge, fixture action adapters and `ops_plane`, the engineering
investigation service, `python3 -m operating workbench` and the console's platform
views) were deleted by reachability; git history has the proofs.
Older root documents that described that platform were removed; git history keeps them.

## Verify

~~~sh
python3 -m unittest <module ...>         # targeted modules; affected tests: python3 tools/affected_tests.py --base origin/main
(cd app && npm test && npm run typecheck)   # the Next app, the one front end
~~~

No governed PLX repository is edited from here, and there is no business write path:
Graph, SharePoint, Portal and BC are read through the GET-only client only.
