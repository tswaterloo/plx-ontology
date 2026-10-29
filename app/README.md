# PLX Workbench web app

Next.js (App Router, TypeScript, Tailwind v4, Radix primitives) front end for
`operator_api`. Screens: sign-in (Microsoft, plus a DEV-mode demo sign-in),
Home (ask box, needs attention, recent, saved), Ask (streaming chat thread with
typo-tolerant autocomplete), answer cards (sources, conflicts, hidden-item
notice, not found, freshness, feedback), object pages and a source reader.

## Run locally

```sh
export PATH=/data/tools/node/bin:$PATH          # Node 20+
npm ci
(cd .. && python3 -m operator_api serve --mock --port 8780 --insecure-cookie) &   # fixture API
npm run dev                                      # http://localhost:3000, /api -> :8780
```

`PLX_API_ORIGIN` points the dev rewrite elsewhere. In production Caddy serves
`/api/*` straight to the API (`deploy/README.md`).

## Contract

`src/lib/openapi.d.ts` is generated from `operator_api/openapi.json`
(`npm run gen:api`); `src/lib/types.ts` names its schemas. `src/lib/contract.test.ts`
fails when the generated file is stale.

## Tests

```sh
npm test            # vitest: components and client boundaries
npm run typecheck
PLAYWRIGHT_BROWSERS_PATH=/data/tools/pw-browsers PLX_API_PYTHON=<python with fastapi> npm run e2e
```

`e2e/demo.spec.ts` walks the demo flow against `operator_api --mock` at
1440 px and 390 px and writes screenshots to `e2e/screenshots/` (gitignored).
