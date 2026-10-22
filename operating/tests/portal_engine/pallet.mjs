// Golden generator for the pallet builder / skid math: runs the Portal's own TypeScript
// (lib/wms/pallet-build.ts, lib/wms/pallet-layout.ts, lib/3pl/skid-math.ts at PORTAL_SRC) on
// the SYNTHETIC cases of a scenarios file and prints every output. Same isolation as run.mjs:
//   env -i PATH="$PATH" TZ=UTC PORTAL_SRC=<portal>/portal/src node \
//     --experimental-transform-types --no-warnings --import ./register.mjs pallet.mjs <scenarios.json>
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";

const SRC = process.env.PORTAL_SRC;
const scenarios = JSON.parse(readFileSync(process.argv[2], "utf8"));
const FILES = ["lib/wms/pallet-build.ts", "lib/wms/pallet-layout.ts", "lib/3pl/skid-math.ts"];
const build = await import(path.join(SRC, FILES[0]));
const skid = await import(path.join(SRC, FILES[2]));

const specOf = (a) => (a.spec ? { ...scenarios.specs[a.spec], ...(a.patch ?? {}) } : undefined);
const FUNCTIONS = {
  casesPerLayer: (a) => build.casesPerLayer(...a),
  dimWeight: (a) => build.dimWeight(...a),
  densityPcf: (a) => build.densityPcf(...a),
  freightClassFromDensity: (a) => a[0].map((pcf) => build.freightClassFromDensity(pcf)),
  buildPallets: (a) => {
    const { spec: _s, patch: _p, ...rest } = a;
    return build.buildPallets({ ...rest, spec: specOf(a) });
  },
  computeSkids: (a) => skid.computeSkids({ cases: a.cases, spec: specOf(a), explicitSkids: a.explicitSkids }),
  caseVolumeCuFt: (a) => skid.caseVolumeCuFt(specOf(a)),
  packSpecDimsComplete: (a) => skid.packSpecDimsComplete(specOf(a)),
};

const out = { portal_commit: process.env.PORTAL_COMMIT ?? null, portal_src_files: {}, results: [] };
for (const rel of FILES) {
  out.portal_src_files["portal/src/" + rel] = createHash("sha256").update(readFileSync(path.join(SRC, rel))).digest("hex");
}
for (const c of scenarios.cases) out.results.push({ fn: c.fn, args: c.args, result: FUNCTIONS[c.fn](c.args) ?? null });
process.stdout.write(JSON.stringify(out, null, 1) + "\n");
