import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { AUTOPILOT_STEPS, checklistFor, GUIDES, guideFor, guideLabel } from "./onboarding";

const registry = JSON.parse(readFileSync(join(__dirname, "../../../operating/autopilot_registry.json"), "utf8")) as {
  autopilots: { id: string; label: string; enabled?: boolean }[];
};
const rules = JSON.parse(readFileSync(join(__dirname, "../../../operating/autopilot_write_rules.json"), "utf8")) as {
  gate: { precision: string };
};

describe("the first-week guides", () => {
  it("cover the seven staff roles with the roster's ids and labels", () => {
    expect(GUIDES.map((g) => g.role).sort()).toEqual(["customer_service", "finance", "production_planning", "purchasing", "quality", "receivables", "rnd"]);
    expect(GUIDES.map((g) => guideLabel(g.role))).toContain("R&D");
    expect(guideFor("payables")).toBeNull();
  });

  it("name only autopilot workflows the registry has switched on", () => {
    const enabled = new Set(registry.autopilots.filter((a) => a.enabled !== false).map((a) => a.id));
    const named = GUIDES.flatMap((g) => g.autopilots);
    expect(named.filter((id) => !enabled.has(id))).toEqual([]);
  });

  it("state the same accuracy bar as the autopilot gate", () => {
    const bar = `${Math.round(Number(rules.gate.precision) * 100)}%`;
    expect(AUTOPILOT_STEPS.filter((s) => s.includes(bar))).toHaveLength(2);
    expect(AUTOPILOT_STEPS.join(" ")).toMatch(/turns itself off/);
    expect(AUTOPILOT_STEPS[0]).toMatch(/off for you when you start/);
  });

  it("give each role its own record and question in the checklist", () => {
    const steps = checklistFor("purchasing");
    expect(steps.map((s) => s.id)).toEqual(["sign_in", "my_work", "look_up", "ask", "autopilot", "feedback"]);
    expect(steps.find((s) => s.id === "look_up")?.hint).toMatch(/PO number/);
    expect(steps.find((s) => s.id === "ask")?.hint).toBe(`For example: ${guideFor("purchasing")?.asks[0]}`);
    expect(checklistFor(null).find((s) => s.id === "look_up")?.hint).toMatch(/order, PO, lot or invoice/);
  });
});
