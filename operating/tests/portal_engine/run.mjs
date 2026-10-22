// Golden generator: runs the Portal's own landed-cost / quote engine (TypeScript at
// PORTAL_SRC) on the representative inputs in a scenarios file and prints every output.
// Usage (Node >= 22.6, credentials stripped):
//   env -i PATH="$PATH" TZ=UTC PORTAL_SRC=<portal>/portal/src node \
//     --experimental-transform-types --no-warnings --import ./register.mjs run.mjs <scenarios.json>
// No network, no database: register.mjs refuses fetch/net/tls/dns/http(s)/child_process/fs
// writes and deletes credential-shaped env vars; the Prisma client is fake_db.mjs, which
// refuses every write.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { recordsHandler } from "./records.mjs";

const SRC = process.env.PORTAL_SRC;
const scenarios = JSON.parse(readFileSync(process.argv[2], "utf8"));
const mod = (p) => import(path.join(SRC, p));
const landed = await mod("lib/costing/landed-cost.ts");
const estimate = await mod("lib/costing/cost-estimate.ts");
const ladder = await mod("lib/costing/margin-ladder.ts");
const overhead = await mod("lib/costing/uptime-overhead.ts");
const tiers = await mod("lib/costing/volume-tiers.ts");
const census = await mod("lib/costing/ready-to-quote.ts");
const bomTotal = await mod("lib/costing/bom-total.ts");
const totals = await mod("lib/quote/totals.ts");
const fallback = await mod("lib/quote/matrix-fallback.ts");
const matrix = await mod("lib/quote/matrix.ts");
const service = await mod("lib/quote/service.ts");
const gl = await mod("lib/procurement/landed-gl.ts");
const engine = await mod("lib/costing/project-landed-quote.ts");

const FUNCTIONS = {
  landedComponentCost: (a) => landed.landedComponentCost(a),
  fillMassKg: (a) => landed.fillMassKg(a.fillWeightG, a.specificGravity),
  estimateUnitCost: (a) => estimate.estimateUnitCost(a),
  resolveMarginLadder: (a) => ladder.resolveMarginLadder(a),
  priceMarginTiers: (a) => ladder.priceMarginTiers(a.cost, ladder.resolveMarginLadder(a.ladder)),
  deriveUptimeOverhead: (a) => overhead.deriveUptimeOverhead(a),
  pickRunVolumeTierForQty: (a) => tiers.pickRunVolumeTierForQty(a.qty),
  pickSupplierCardTierPrice: (a) => engine.pickSupplierCardTierPrice(a),
  allocatePoFreightPerUnit: (a) => engine.allocatePoFreightPerUnit(a),
  deriveReadyToQuote: (a) => census.deriveReadyToQuote(a),
  rollupBomTotal: (a) => bomTotal.rollupBomTotal(a.lines),
  computeQuoteTotals: (a) => totals.computeQuoteTotals(a.lineTotals, a.taxRatePercent),
  deriveUnitPrice: (a) => totals.deriveUnitPrice(a.stdCost, a.marginPercent),
  resolveUnitPrice: (a) => totals.resolveUnitPrice(a),
  lineTotal: (a) => totals.lineTotal(a.unitPrice, a.quantity),
  fallbackMatrixUnitCost: (a) => fallback.fallbackMatrixUnitCost(a),
  applyColumnUnitCostFallback: (a) => fallback.applyColumnUnitCostFallback(a.column, a.lineStdCost, a.theoreticalFgCost),
  buildLandedExpenseLines: (a) => gl.buildLandedExpenseLines(a),
  resolvePostedLandedAmounts: (a) => gl.resolvePostedLandedAmounts(a),
  customerSuppliedRequirements: (a) => engine.customerSuppliedRequirements(a.materials, a.units),
};

// The scenario's records answer the engine's reads with Prisma semantics (records.mjs); the
// quote run quantity is given directly (defaultLoadQuoteRunQty's nested select).
function handlerFor(db) {
  const read = recordsHandler(db);
  return (model, method, args) => {
    if (model === "quote" && method === "findFirst") {
      return db.quoteRunQty != null ? { lines: [{ quantity: db.quoteRunQty }] } : null;
    }
    return read(model, method, args);
  };
}

const out = { portal_commit: process.env.PORTAL_COMMIT ?? null, portal_src_files: {}, functions: {}, scenarios: {} };
for (const rel of ["lib/costing/landed-cost.ts", "lib/costing/cost-estimate.ts", "lib/costing/rate-masters.ts",
  "lib/costing/margin-ladder.ts", "lib/costing/uptime-overhead.ts", "lib/costing/volume-tiers.ts",
  "lib/costing/template-cost-matrix.ts", "lib/costing/ready-to-quote.ts", "lib/costing/bom-total.ts",
  "lib/costing/project-landed-quote.ts", "lib/costing/raw-cost.ts", "lib/costing/formulation-resolution.ts",
  "lib/costing/fg-routing.ts", "lib/costing/fg-routing-store.ts", "lib/costing/setup-standards.ts",
  "lib/costing/setup-standards-store.ts", "lib/costing/draft-cost-overrides.ts",
  "lib/assembly-bom/draft-cost-persist.ts", "lib/quote/totals.ts", "lib/quote/matrix-fallback.ts",
  "lib/quote/matrix.ts", "lib/quote/service.ts", "lib/procurement/landed-gl.ts",
  "lib/customer-materials/supply-source-shared.ts", "lib/customer-materials/supply-source.ts",
  "lib/customers/anchor.ts", "lib/sourcing/raw-product.ts", "lib/sourcing/status.ts"]) {
  out.portal_src_files["portal/src/" + rel] = createHash("sha256").update(readFileSync(path.join(SRC, rel))).digest("hex");
}
for (const [name, cases] of Object.entries(scenarios.functions)) {
  out.functions[name] = cases.map((args) => ({ args, result: FUNCTIONS[name](structuredClone(args)) }));
}
for (const scenario of scenarios.engine) {
  globalThis.__DB_CALLS.length = 0;
  globalThis.__FAKE_DB = handlerFor(scenario.db);
  const result = await engine.quoteProjectLanded(scenario.project_ref, {
    persist: false, askedVolumes: scenario.asked_volumes ?? undefined,
  });
  const offer = scenario.offer ? (() => {
    const lineStd = fallback.firstPositiveLineStdCost(scenario.offer.quoteLines);
    const theoretical = result.estimate?.totalCostPerUnit ?? null;
    const priced = result.volumeMatrix.map((column) => fallback.applyColumnUnitCostFallback(column, lineStd, theoretical));
    const lines = priced.map(({ column }) => {
      const marginPercent = matrix.resolveOfferMarginPct({ column, selections: scenario.offer.selections,
        quoteMarginTargetPct: scenario.offer.quoteMarginTargetPct });
      return { quoteVolumeId: `vol-${column.askedUnits}`, askedUnits: column.askedUnits,
        unitCost: column.unitCost, marginPercent, unitPrice: matrix.priceColumnAtMargin(column, marginPercent) };
    });
    const creates = service.buildOfferLineCreates("quote-1", { productName: "Offer", lines });
    return { lineStdCost: lineStd, sources: priced.map((p) => p.source), lines,
      creates: creates.map((c) => ({ quantity: c.quantity, unitPrice: c.unitPrice, totalPrice: c.totalPrice,
        marginPercent: c.marginPercent })),
      totals: totals.computeQuoteTotals(creates.map((c) => c.totalPrice ?? 0), scenario.offer.taxRatePercent) };
  })() : null;
  const calls = [...new Set(globalThis.__DB_CALLS.map((c) => `${c.model}.${c.method}`))].sort();
  out.scenarios[scenario.name] = { result, offer, db_calls: calls };
}
process.stdout.write(JSON.stringify(out, null, 1) + "\n");
