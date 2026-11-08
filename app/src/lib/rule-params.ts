// The compiled rule's parameters as plain rows, shown back before a rule is kept, so the
// reader confirms what will run (the spec is data the server validated; nothing here runs).
export type RuleParam = { name: string; value: string };

const OPS: Record<string, string> = {
  changed: "changes", later: "moves later", earlier: "moves earlier", increased: "goes up",
  decreased: "goes down", becomes: "becomes", gt: "over", gte: "at least", lt: "under",
  lte: "at most", eq: "is", ne: "is not",
};

const words = (id: string) => id.replace(/_/g, " ");

export function ruleParams(spec: unknown): RuleParam[] {
  if (!spec || typeof spec !== "object") return [];
  const s = spec as Record<string, any>;
  const out: RuleParam[] = [];
  if (s.signal && typeof s.signal === "object") {
    const g = s.signal as Record<string, any>;
    out.push({ name: "Findings", value: (Array.isArray(g.kinds) ? g.kinds : []).map(words).join(", ") });
    if (g.min_amount != null) out.push({ name: "Amount at least", value: `${g.min_amount}${g.currency ? ` ${g.currency}` : " (finding's own currency)"}` });
    if (g.priority) out.push({ name: "Priority", value: g.priority === "high" ? "high only" : `${g.priority} or higher` });
    return out;
  }
  if (typeof s.subject === "string") out.push({ name: "Records", value: s.subject });
  for (const c of Array.isArray(s.when) ? s.when : []) {
    const value = c.value != null ? ` ${c.value}${c.currency ? ` ${c.currency}` : ""}` : "";
    out.push({ name: words(String(c.property)), value: `${OPS[c.op] ?? c.op}${value}` });
  }
  if (s.count && typeof s.count === "object") {
    out.push({ name: "Count", value: `${s.count.at_least} times within ${s.count.within_days} days${s.count.per ? ` per ${words(String(s.count.per).split(".").pop() ?? "")}` : ""}` });
  }
  return out;
}
