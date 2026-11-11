import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ days: vi.fn(), area: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getAutopilotDays: api.days, getAutopilot: api.area }));

const ME = "33333333-3333-4333-8333-333333333333";
const gateRow = (workflow: string, countdown: unknown, person = ME) => ({
  role: "person:ana@plx.example", person, workflow, dev: null, fresh: null, n: 0, state: "dry_run", on: true, live_flag: false,
  missing: [], last_run: null, scored_at: null, open_items: 0, draft: true, countdown,
});

import { AutopilotScorecardPanel } from "./autopilot-scorecard";

const day = (d: string, prepared = 0, accepted = 0, edited = 0, rejected = 0) => ({ day: d, prepared, accepted, edited, rejected });

// braces: a hook that returns the mock would have vitest call it as a teardown
beforeEach(() => {
  api.days.mockReset();
  api.area.mockReset();
  api.area.mockResolvedValue({
    people: [{ id: ME, name: "Ana", you: true, roles: [] }],
    workflows: [
      gateRow("internal_approval", { judged: 12, matched: 11, need_judged: 15, need_percent: 90, percent: 91.7, passes: false, window: [], data_end: null }),
      gateRow("mc_question", null),
      gateRow("teams_chat", null, "55555555-5555-4555-8555-555555555555"),
    ],
  });
});

describe("autopilot scorecard in the work log", () => {
  it("one row a day with prepared, accepted, edited and rejected, and the share used", async () => {
    api.days.mockResolvedValue({ available: true, days: [day("2026-11-11", 9, 4, 2, 1), day("2026-11-10", 3, 1, 0, 2), day("2026-11-09")] });
    render(<AutopilotScorecardPanel days={3} />);
    const panel = await screen.findByTestId("autopilot-scorecard");
    expect(panel).toHaveTextContent("Autopilot, last 3 days");
    expect(panel).toHaveTextContent("7 of 10 decided items used (70%), 2 after an edit");
    const rows = panel.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Today9421");
    expect(rows[2]).toHaveTextContent("––––");
    expect(api.days).toHaveBeenCalledWith(3, expect.anything());
    // the gate per drafted workflow of yours, in judged items
    const gates = await screen.findByTestId("scorecard-gates");
    expect(gates).toHaveTextContent("12 of 15 items at ≥90% · 91.7% right so far");
    expect(gates).toHaveTextContent("No fresh items judged yet");
    expect(gates).not.toHaveTextContent("Teams chat");
  });

  it("stays hidden while nothing was prepared or the people list is not loaded", async () => {
    api.days.mockResolvedValue({ available: true, days: [day("2026-11-11")] });
    const { unmount } = render(<AutopilotScorecardPanel />);
    await Promise.resolve();
    expect(screen.queryByTestId("autopilot-scorecard")).not.toBeInTheDocument();
    unmount();
    api.days.mockResolvedValue({ available: false, days: [] });
    render(<AutopilotScorecardPanel />);
    await Promise.resolve();
    expect(screen.queryByTestId("autopilot-scorecard")).not.toBeInTheDocument();
  });
});
