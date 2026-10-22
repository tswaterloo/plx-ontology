# Decision 2026-10-07: Portal routes whose only data is the shared auth layer are auth_layer_only

- raised: 2026-10-07 (gap-d75984d1b5fa, 53 Portal
  api routes `models_via_shared`; gap-31ffa7fc934a, 6 routes `reaches_data_client`)
- decided: 2026-10-08
- status: decided

## Context

`scripts/source_registry.py` treats a module more than half of all Portal routes reach as the
shared auth/audit layer (`portal/src/lib/auth/*`, `portal/src/lib/db/*`). At plx-customer-portal
48d76e8 the 53 `models_via_shared` routes reached only that layer's models (User, Account,
SecurityEvent, TemporaryElevationGrant/Request). About 20 of them also query a data client of
their own (e.g. `portal/src/app/api/fm/po/route.ts:14`, raw SQL `FROM purchase_order` through
`queryFM`), which the generator did not record: it computed data clients only for routes that
reached no shared data, and dropped an unqualified SQL table absent from the reporting manifest.

## Decision

1. Every route without data of its own lists the data clients its own (non-shared) closure
   holds or imports from the shared layer itself (through re-export modules), and every SQL
   table no Prisma model or reporting relation resolves is kept as `unresolved_relations`.
   Such a route stays declared with reason `reaches_data_client` (or `unresolved_relation`);
   a route with data and an unresolved table stops at declared with `unresolved_relation`.
2. A route whose only data is the shared auth layer's (no own data, data client or
   unresolved table) gets the terminal status `auth_layer_only`: it is not a data source of
   its own. The auth layer's models stay tracked as their `prisma_model` entries; the route
   keeps `models_via_shared` in the registry.
3. Of the 6 former `reaches_data_client` routes, `external/swarm/v1/{query,schema,summary}`
   stay declared through their data client (`portal/src/lib/db/db.ts`; generic queries over
   any visible Prisma model). Out of scope, by entry id in `ontology/source_scope.json`:
   `api/health` (a `SELECT 1` liveness probe), `api/cron/webhook-renewal` (renews Graph
   webhook subscriptions and alerts Teams: operations, not business records) and
   `api/rfq/submit` (a public write of vendor quotes to a SharePoint list; the list is
   covered, if at all, by SharePoint capture, not by the route).

The rule is general: no route is named in code. If this record is missing or not
`status: decided`, an auth-only route falls back to declared with reason `models_via_shared`.
