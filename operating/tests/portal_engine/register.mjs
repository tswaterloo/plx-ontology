import { register, createRequire, syncBuiltinESMExports } from "node:module";

// Isolation for the golden harness: no credentials, no network, no child processes, no file
// writes. The Portal modules it loads include Business Central / Graph clients; none of their
// I/O paths is reached by the scenarios, and these guards make sure none can be.
const CREDENTIAL = /^(BC_|AZURE_|MICROSOFT_|SHAREPOINT_|GRAPH|PLX_WORKBENCH|VMC_|MC_|AWS_|DATABASE_URL|DIRECT_URL|DOCUSIGN|RESEND|NEXTAUTH|OPENAI|ANTHROPIC|GITHUB|GH_)/i;
for (const key of Object.keys(process.env)) {
  if (CREDENTIAL.test(key) || /(SECRET|TOKEN|PASSWORD|API_KEY)/i.test(key)) delete process.env[key];
}
const refuse = (what) => () => { throw new Error(`portal golden harness: ${what} refused`); };
globalThis.fetch = refuse("fetch");
globalThis.WebSocket = undefined;
const require = createRequire(import.meta.url);
for (const [mod, names] of Object.entries({
  net: ["connect", "createConnection", "createServer"],
  tls: ["connect", "createServer"],
  dns: ["lookup", "resolve", "resolve4", "resolve6"],
  http: ["request", "get", "createServer"],
  https: ["request", "get", "createServer"],
  http2: ["connect", "createServer"],
  child_process: ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"],
  fs: ["writeFile", "writeFileSync", "appendFile", "appendFileSync", "mkdir", "mkdirSync",
    "rm", "rmSync", "unlink", "unlinkSync", "rename", "renameSync", "createWriteStream"],
})) {
  const target = require(mod);
  for (const name of names) target[name] = refuse(`${mod}.${name}`);
}
require("dns").promises.lookup = refuse("dns.promises.lookup");
require("fs").promises.writeFile = refuse("fs.promises.writeFile");
syncBuiltinESMExports();

// An inert stand-in for any package: every property is itself, calls return itself.
function stub() {
  const target = function () {};
  const proxy = new Proxy(target, {
    get(_t, key) {
      if (key === "then") return undefined;
      if (key === Symbol.toPrimitive) return () => "";
      if (key === "prototype") return {};
      return proxy;
    },
    apply() { return proxy; },
    construct() { return {}; },
  });
  return proxy;
}
globalThis.__stub = () => stub();
register("./hooks.mjs", import.meta.url);
