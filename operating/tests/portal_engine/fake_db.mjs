// A read-only fake of the Prisma client. Reads are answered from globalThis.__FAKE_DB
// (model, method, args) -> value | undefined; anything that would write throws and is recorded.
const WRITE = /^(create|createMany|update|updateMany|upsert|delete|deleteMany|\$executeRaw|\$executeRawUnsafe|\$transaction)$/;
export const calls = [];
globalThis.__DB_CALLS = calls;

function model(name) {
  return new Proxy({}, {
    get(_t, method) {
      if (method === "then") return undefined;
      return async (args) => {
        calls.push({ model: name, method: String(method) });
        if (WRITE.test(String(method))) throw new Error(`write refused: ${name}.${String(method)}`);
        const handler = globalThis.__FAKE_DB;
        const value = handler ? handler(name, String(method), args ?? {}) : undefined;
        if (value !== undefined) return structuredClone(value);
        if (String(method).startsWith("findMany")) return [];
        if (method === "count") return 0;
        return null;
      };
    },
  });
}

export const db = new Proxy({}, {
  get(_t, key) {
    if (key === "then") return undefined;
    if (typeof key === "string" && key.startsWith("$")) {
      return async () => {
        calls.push({ model: key, method: "" });
        if (WRITE.test(key)) throw new Error(`write refused: ${key}`);
        return [];
      };
    }
    return model(key);
  },
});
export default db;
