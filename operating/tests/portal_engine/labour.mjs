// Golden generator for the labour-hour derivation, the formula and ASSEMBLY BOM selection and
// the raw-material cost of the Portal's quote engine (LOG-08): runs the Portal's own TypeScript
// at PORTAL_SRC on the SYNTHETIC cases of a scenarios file and prints every output. Same
// isolation as run.mjs (no network, no writes, a fake read-only Prisma client over records.mjs):
//   env -i PATH="$PATH" TZ=UTC PORTAL_SRC=<portal>/portal/src node \
//     --experimental-transform-types --no-warnings --import ./register.mjs labour.mjs <scenarios.json>
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { recordsHandler } from "./records.mjs";

const SRC = process.env.PORTAL_SRC;
const scenarios = JSON.parse(readFileSync(process.argv[2], "utf8"));
const FILES = ["lib/costing/fg-routing.ts", "lib/costing/template-cost-matrix.ts",
  "lib/costing/setup-standards.ts", "lib/costing/setup-standards-store.ts",
  "lib/costing/fg-routing-store.ts", "lib/costing/draft-cost-overrides.ts",
  "lib/assembly-bom/draft-cost-persist.ts", "lib/costing/formulation-resolution.ts",
  "lib/costing/raw-cost.ts", "lib/costing/bom-total.ts", "lib/costing/volume-tiers.ts",
  "lib/costing/project-landed-quote.ts"];
const mod = (p) => import(path.join(SRC, p));
const [routing, matrix, setup, routingStore, persist, resolution, raw, bomTotal, engine] =
  await Promise.all([FILES[0], FILES[1], FILES[2], FILES[4], FILES[6], FILES[7], FILES[8], FILES[9],
    FILES[11]].map(mod));

const store = (drafts) => (drafts == null ? null : { compounding: [], assembly: [], drafts });
const attempt = (fn) => {
  try {
    return fn() ?? null;
  } catch (err) {
    return { error: err.message };
  }
};

const FUNCTIONS = {
  quoteLabourFromRouting: (a) => routing.quoteLabourFromRouting(a.routing, a.assemblyRate, a.opts),
  assemblyLabourPerUnit: (a) => matrix.assemblyLabourPerUnit(a.headcount, a.rateBpm),
  compoundingLabourPerUnit: (a) => matrix.compoundingLabourPerUnit(a.compounderCount, a.hours, a.yieldUnits),
  amortiseSetupHours: (a) => setup.amortiseSetupHours(a),
  compoundingCrewHoursPerUnit: (a) => setup.compoundingCrewHoursPerUnit(a),
  resolveSetupStandard: (a) => setup.resolveSetupStandard(a.rows, a.query),
  resolveQuoteHours: (a) => engine.resolveQuoteHours({ ...a, draftStore: store(a.drafts) }),
  resolveExpectedUnitsOverride: (a) =>
    engine.resolveExpectedUnitsOverride(store(a.drafts), a.projectKey, a.pricedTierCode),
  buildVolumeMatrixColumn: (a) => {
    const column = engine.buildVolumeMatrixColumn({
      askedUnits: a.askedUnits,
      facts: { project: { projectKey: a.projectKey }, componentLines: [], rawMaterials: [], rates: a.rates,
        customerSupplied: [] },
      fillMassKg: a.fillMassKg, standard: a.standard, standardDbError: false,
      setupStandard: a.setupStandard, routing: a.routing, draftStore: store(a.drafts),
      assemblyRate: a.assemblyRate,
    });
    return { labour: column.labour, charges: column.vesselPlan.pick?.charges ?? null, overhead: column.overhead };
  },
  rawCostPerKg: (a) => raw.rawCostPerKg(a),
  rollupBomTotal: (a) => bomTotal.rollupBomTotal(a.lines),
};

// Reads through the fake client: the Portal's queries select among the scenario's records.
const READS = {
  resolveQuoteFormulation: async (a) => {
    const r = await resolution.resolveQuoteFormulation({ ...a, warn: () => {} });
    return { source: r.source, formulationId: r.formulation?.id ?? null,
      rawCostPerKg: (r.formulation?.ingredients ?? []).map((i) => raw.rawCostPerKg(i.product)) };
  },
  gatherQuoteFacts: async (ref) => {
    const f = await engine.gatherQuoteFacts(ref);
    if (!f) return null;
    return { project: f.project, resolutionSource: f.resolved.source,
      formulationId: f.resolved.formulation?.id ?? null, specificGravity: f.specificGravity,
      fillWeightG: f.fillWeightG, bomTotal: f.bomTotal, rawMaterials: f.rawMaterials,
      componentLines: f.componentLines.map((l) => ({ lineKey: l.lineKey, lineName: l.lineName,
        qtyPerUnit: l.qtyPerUnit, linked: l.linked, pricedUnitCost: l.pricedUnitCost })),
      fgTargetQty: f.fgTargetQty, unsourcedLineCount: f.unsourcedLineCount,
      linkedLineCount: f.linkedLineCount };
  },
  getPrimaryRouting: async (key) => {
    const r = await routingStore.getPrimaryRouting(key, { persist: false });
    return r && { id: r.id, standardHoursPerUnit: r.standardHoursPerUnit,
      standardSetupMinutes: r.standardSetupMinutes, standardChangeoverMinutes: r.standardChangeoverMinutes,
      totalOperators: r.totalOperators };
  },
  loadDraftCostStore: async (key) => (await persist.loadDraftCostStore(key)).drafts,
};

const out = { portal_commit: process.env.PORTAL_COMMIT ?? null, portal_src_files: {}, functions: {}, records: {} };
for (const rel of FILES) {
  out.portal_src_files["portal/src/" + rel] = createHash("sha256").update(readFileSync(path.join(SRC, rel))).digest("hex");
}
for (const [name, cases] of Object.entries(scenarios.functions)) {
  out.functions[name] = cases.map((args) => ({ args, result: attempt(() => FUNCTIONS[name](structuredClone(args))) }));
}
for (const scenario of scenarios.records) {
  globalThis.__DB_CALLS.length = 0;
  globalThis.__FAKE_DB = recordsHandler(scenario.db, scenario.fail ?? []);
  const results = {};
  for (const [name, input] of Object.entries(scenario.calls)) {
    try {
      results[name] = await READS[name](structuredClone(input));
    } catch (err) {
      results[name] = { error: err.message };
    }
  }
  const calls = [...new Set(globalThis.__DB_CALLS.map((c) => `${c.model}.${c.method}`))].sort();
  out.records[scenario.name] = { results, db_calls: calls };
}
process.stdout.write(JSON.stringify(out, null, 1) + "\n");
