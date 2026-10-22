// Read-only answers over a scenario's Prisma-shaped records, for the fake client in fake_db.mjs.
// The Prisma semantics the costing engine relies on are applied, not assumed: `where` (equality,
// null, {in}, {notIn} (SQL NOT IN: a null never matches), {not}, {gt}, OR, a to-one relation
// filter), `orderBy` (fields or a relation path,
// asc/desc) and the `include`/`select` of relation lists (their own where/orderBy/take), so the
// Portal's own queries pick the records (formula pin, FG pin, ASSEMBLY BOM, primary routing,
// supplier cards). `select` does not project scalar fields. A model a scenario does not hold
// answers like an empty table; a model listed in `fail` throws, as a failed read.
const OPS = new Set(["in", "notIn", "not", "gt", "equals"]);
const isPlain = (v) => v !== null && typeof v === "object" && !Array.isArray(v) && !(v instanceof Date);

function matches(row, where) {
  if (!where) return true;
  for (const [key, cond] of Object.entries(where)) {
    if (key === "OR") {
      if (!cond.some((c) => matches(row, c))) return false;
      continue;
    }
    const value = row?.[key] ?? null;
    if (isPlain(cond) && Object.keys(cond).some((k) => OPS.has(k))) {
      if ("equals" in cond && value !== cond.equals) return false;
      if ("in" in cond && !cond.in.includes(value)) return false;
      if ("notIn" in cond && (value === null || cond.notIn.includes(value))) return false;
      if ("gt" in cond && !(value != null && value > cond.gt)) return false;
      if ("not" in cond && (cond.not === null ? value === null : value === null || value === cond.not)) {
        return false;
      }
    } else if (isPlain(cond)) {
      if (value === null || !matches(value, cond)) return false; // to-one relation filter
    } else if (value !== cond) {
      return false;
    }
  }
  return true;
}

function orderKeys(orderBy) {
  const out = [];
  for (const entry of [orderBy ?? []].flat()) {
    for (const [field, dir] of Object.entries(entry)) {
      if (isPlain(dir)) {
        for (const [inner, d] of Object.entries(dir)) out.push({ path: [field, inner], desc: d === "desc" });
      } else {
        out.push({ path: [field], desc: dir === "desc" });
      }
    }
  }
  return out;
}

function order(rows, orderBy) {
  const keys = orderKeys(orderBy);
  if (keys.length === 0) return rows;
  const at = (row, path) => path.reduce((v, k) => (v == null ? v : v[k]), row);
  return [...rows].sort((a, b) => {
    for (const { path, desc } of keys) {
      const x = at(a, path);
      const y = at(b, path);
      const c = x < y ? -1 : x > y ? 1 : 0;
      if (c !== 0) return desc ? -c : c;
    }
    return 0;
  });
}

function shape(row, spec) {
  if (row == null || !spec) return row;
  const nested = { ...(spec.include ?? {}), ...(spec.select ?? {}) };
  const out = { ...row };
  for (const [key, sub] of Object.entries(nested)) {
    if (!isPlain(sub)) continue;
    const value = row[key];
    if (Array.isArray(value)) {
      let list = order(value.filter((r) => matches(r, sub.where)), sub.orderBy);
      if (sub.take != null) list = list.slice(0, sub.take);
      out[key] = list.map((r) => shape(r, sub));
    } else if (isPlain(value)) {
      out[key] = shape(value, sub);
    }
  }
  return out;
}

const DATE_FIELDS = { projectLocal: ["createdAt", "updatedAt"] };

export function recordsHandler(records, fail = []) {
  return (model, method, args) => {
    if (fail.includes(model)) throw new Error(`fake read failed: ${model}`);
    const rows = records[model];
    if (!Array.isArray(rows)) return undefined;
    const hits = order(rows.filter((r) => matches(r, args.where)), args.orderBy)
      .map((r) => shape(r, args))
      .map((r) => {
        const dates = DATE_FIELDS[model] ?? [];
        return dates.length ? { ...r, ...Object.fromEntries(dates.map((f) => [f, new Date(r[f])])) } : r;
      });
    if (method === "findMany") return hits;
    if (method === "findFirst" || method === "findUnique") return hits[0] ?? null;
    return undefined;
  };
}
