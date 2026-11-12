import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { TrustPage } from "@/lib/types";

const api = vi.hoisted(() => ({ trust: vi.fn() }));
vi.mock("@/components/command-palette", () => ({ useCommandPalette: () => ({ open: () => undefined }) }));
vi.mock("next/navigation", () => ({ usePathname: () => "/trust", useRouter: () => ({ replace: () => undefined, push: () => undefined }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getTrust: api.trust }));

import TrustRoute from "./page";

const row = (over: Record<string, unknown> = {}) => ({
  role: "person:vince@plx.example", person: null, workflow: "teams_chat", dev: { judged: 51, matched: 45, percent: 88.2 }, fresh: null, n: 51,
  state: "dry_run" as const, on: true, live_flag: false, missing: ["gate_not_passed"], last_run: null, scored_at: null, open_items: 0, draft: true,
  countdown: { judged: 12, matched: 11, need_judged: 15, need_percent: 90, percent: 91.7, passes: false, window: ["2026-11-06T07:00:00Z", "2026-11-12T07:00:00Z"], data_end: null },
  ...over,
});

const page = (over: Partial<TrustPage> = {}): TrustPage => ({
  checked_at: "2026-11-11T20:00:00Z",
  freshness: [
    { system: "BC", captured_at: "2026-11-11T18:00:00Z", served_at: "2026-11-11T16:00:00Z", lag_seconds: 7200, stale: false, capture: "complete" },
    { system: "Email", captured_at: "2026-11-11T18:30:00Z", served_at: "2026-11-07T06:45:00Z", lag_seconds: 387_900, stale: true, capture: "partial" },
  ],
  answers: [],
  alert_rule: "A type is on by default only when measured.",
  alerts: [
    { type: "po_late", kind: "late_po", label: "Late purchase order", on: true, home: true, judged: 15, correct: 15, percent: 100, measured_on: "2026-11-08", note: null },
    { type: "customer_order_late", kind: "customer_order_late", label: "Customer order late", on: false, home: false, judged: 59, correct: null, percent: 21, measured_on: "2026-11-08", note: null },
    { type: "po_due", kind: "late_po", label: "Due today, not confirmed", on: false, home: false, judged: null, correct: null, percent: null, measured_on: null, note: "not measurable yet" },
  ],
  autopilot: [row(), row({ workflow: "mc_question", countdown: null, dev: { judged: 22, matched: 18, percent: 81.8 } })],
  autopilot_note: null,
  autopilot_gate: { need_judged: 15, need_percent: 90 },
  lines: [
    { state: "live", words: "Reading BC, Email, read only." },
    { state: "gated", words: "Autopilot sends and writes: a workflow acts only after 15 judged fresh items at 90% or better and the owner's switch." },
    { state: "pending", words: "Held-out answer scores: not measured yet." },
  ],
  switches: [
    { system: "Email and Teams messages", state: "off", words: "Off: nothing is sent." },
    { system: "Business Central", state: "off", words: "Off: approved writes are recorded as dry runs." },
    { system: "Mission Control", state: "none", words: "Read only: the Workbench has no write path here." },
  ],
  ...over,
});

// braces: a hook that returns the mock would have vitest call it as a teardown
beforeEach(() => {
  api.trust.mockReset();
});

describe("Trust page", () => {
  it("shows freshness with its lag, unmeasured scores as not measured, the countdown and every switch", async () => {
    api.trust.mockResolvedValue(page());
    render(<TrustRoute />);
    const fresh = await screen.findByTestId("trust-freshness");
    const bc = within(fresh).getByText("BC").closest("tr")!;
    expect(bc).toHaveTextContent("2 h");
    expect(within(fresh).getByText("Email").closest("tr")).toHaveTextContent("Behind");
    expect(screen.getByTestId("trust-answers-empty")).toHaveTextContent("Not measured yet");
    const auto = screen.getByTestId("trust-autopilot");
    const teams = within(auto).getByText("Teams chat").closest("tr")!;
    expect(teams).toHaveTextContent("Vince");
    expect(teams).toHaveTextContent("Draft");
    expect(teams).toHaveTextContent("88.2%");
    expect(teams).toHaveTextContent("12 of 15 items at ≥90% · 91.7% right so far");
    expect(within(auto).getByText(/question/i).closest("tr")).toHaveTextContent("not scored yet");
    // a rate with no whole count shows no invented count
    const rough = within(screen.getByTestId("trust-alerts")).getByText("Customer order late").closest("tr")!;
    expect(rough).toHaveTextContent("59");
    expect(rough).toHaveTextContent("21.0%");
    expect(rough).toHaveTextContent("—");
    expect(screen.getByText("1 types not measured yet (off until hand-checked)")).toBeInTheDocument();
    const sw = screen.getByTestId("trust-switches");
    expect(within(sw).getByText("Business Central").closest("tr")).toHaveTextContent("Off");
    expect(within(sw).getByText("Mission Control").closest("tr")).toHaveTextContent("None");
    expect(screen.getByTestId("trust-lines")).toHaveTextContent("Gated");
  });

  it("says when the newest scorer run was not counted, beside the older counts", async () => {
    const older = { judged: 12, matched: 11, need_judged: 15, need_percent: 90, percent: 91.7, passes: false, window: [], data_end: null, run: "run-a", refused: "status:partial", refused_run: "run-b" };
    api.trust.mockResolvedValue(page({ autopilot: [row({ countdown: older })] }));
    render(<TrustRoute />);
    const auto = await screen.findByTestId("trust-autopilot");
    expect(within(auto).getByText("Teams chat").closest("tr")).toHaveTextContent(
      "12 of 15 items at ≥90% · 91.7% right so far · newest scorer run not counted (status:partial): these counts are from an earlier run",
    );
  });

  it("words the gate from the server's rules, and says nothing acts when they are unreadable", async () => {
    api.trust.mockResolvedValue(page({ autopilot_gate: { need_judged: 20, need_percent: 95 } }));
    const { unmount } = render(<TrustRoute />);
    expect(await screen.findByText(/at least 20 items judged on fresh days, 95% or more right/)).toBeInTheDocument();
    expect(screen.getByText("Gate: fresh items at ≥95%")).toBeInTheDocument();
    expect(screen.queryByText(/at least 15 items/)).toBeNull();
    unmount();
    api.trust.mockResolvedValue(page({ autopilot_gate: null }));
    render(<TrustRoute />);
    expect(await screen.findByText(/Gate: the rules cannot be read, so no workflow acts\./)).toBeInTheDocument();
  });

  it("shows held-out and development scores with date and sample size", async () => {
    const score = { at: "2026-11-10T15:00:00Z", runs: 12, items: 12, correct: 9, answered: 10, accuracy: 90, coverage: 83.3, evidence_label: "live-captures" };
    api.trust.mockResolvedValue(page({ answers: [{ name: "ask_general", dev: { ...score, runs: 40, items: 20 }, held_out: score }] }));
    render(<TrustRoute />);
    const table = await screen.findByTestId("trust-answers");
    const held = within(table).getByLabelText("Held-out score");
    expect(held).toHaveTextContent("90.0%");
    expect(held).toHaveTextContent("9 of 10 right · 12 questions, 12 runs · Nov 10");
  });

  it("a reader the server refuses sees page not found, nothing of the page", async () => {
    api.trust.mockRejectedValue(new ApiError(404, "not_found"));
    render(<TrustRoute />);
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    expect(screen.queryByTestId("trust-page")).not.toBeInTheDocument();
  });
});
