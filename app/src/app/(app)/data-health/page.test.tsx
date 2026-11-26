import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { TrustFreshness, TrustPage } from "@/lib/types";

const api = vi.hoisted(() => ({ trust: vi.fn(), health: vi.fn() }));
vi.mock("@/components/command-palette", () => ({ useCommandPalette: () => ({ open: () => undefined }) }));
vi.mock("next/navigation", () => ({ usePathname: () => "/data-health", useRouter: () => ({ replace: () => undefined, push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getTrust: api.trust, getDataHealth: api.health, getFeedback: () => Promise.resolve({ all_readers: false, total: 0, offset: 0, counts: {}, entries: [] }) }));

import TrustRoute from "./page";
const DataHealthPage = TrustRoute;

const row = (over: Record<string, unknown> = {}) => ({
  role: "person:vince@plx.example", person: null, workflow: "teams_chat", dev: { judged: 51, matched: 45, percent: 88.2 }, fresh: null, n: 51,
  state: "dry_run" as const, on: true, switched_on: 0, missing: ["gate_not_passed"], last_run: null, scored_at: null, open_items: 0, draft: true,
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
    { type: "customer_order_late", kind: "customer_order_late", label: "Customer order past its required date", on: false, home: false, judged: null, correct: null, percent: null, measured_on: null, note: "not measurable yet" },
    { type: "customer_order_slower_than_usual", kind: "customer_order_slower", label: "Customer order later than this customer's orders usually are", on: false, home: false, judged: 59, correct: null, percent: 21, measured_on: "2026-11-08", note: null },
    { type: "po_due", kind: "late_po", label: "Due today, not confirmed", on: false, home: false, judged: null, correct: null, percent: null, measured_on: null, note: "not measurable yet" },
  ],
  autopilot: [row(), row({ workflow: "mc_question", countdown: null, dev: { judged: 22, matched: 18, percent: 81.8 } })],
  autopilot_note: null,
  autopilot_gate: { need_judged: 15, need_percent: 90 },
  lines: [
    { state: "live", words: "Reading BC, Email, read only." },
    { state: "gated", words: "Autopilot sends and writes: a workflow acts only after 15 judged held-out items at 90% or better, and only for a person who switched it on for themselves." },
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
  api.health.mockReset();
  api.health.mockResolvedValue({ status: "unread", checks: [], partial: false });
});

describe("Data health page", () => {
  it("is one Admin page titled Data health, with records to fix beside freshness and scores, and no Trust repeats", async () => {
    api.trust.mockResolvedValue(page());
    render(<DataHealthPage />);
    expect(await screen.findByTestId("data-health-page")).toBeInTheDocument();
    expect(await screen.findByTestId("trust-freshness")).toBeInTheDocument();
    // a records read that failed is said with a retry, never a list of "None"
    expect(await screen.findByText(/couldn.t be read just now/)).toBeInTheDocument();
    expect(screen.queryByText("None")).toBeNull();
    expect(screen.queryByTestId("trust-lines")).toBeNull();
    expect(screen.queryByText("New rule")).toBeNull();
  });

  it("starts the records read with the page, before the trust read answers", async () => {
    api.trust.mockReturnValue(new Promise(() => undefined));
    render(<DataHealthPage />);
    await vi.waitFor(() => expect(api.health).toHaveBeenCalled());
    expect(api.trust).toHaveBeenCalled();
    expect(screen.queryByTestId("trust-freshness")).toBeNull();
  });

  it("lists the systems with a problem first, in plain words, under Needs attention", async () => {
    api.trust.mockResolvedValue(page());
    render(<DataHealthPage />);
    const attention = await screen.findByTestId("trust-summary");
    expect(within(attention).getAllByRole("listitem")[0]).toHaveTextContent(/^Mail is behind/);
    const rows = within(screen.getByTestId("trust-freshness")).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Mail");
    expect(rows[0]).toHaveTextContent("Behind");
  });

  it("shows freshness with its lag, unmeasured scores as not measured, the countdown and every switch", async () => {
    api.trust.mockResolvedValue(page());
    render(<TrustRoute />);
    const fresh = await screen.findByTestId("trust-freshness");
    const bc = within(fresh).getByText("Business Central").closest("tr")!;
    expect(bc).toHaveTextContent("2 h ago");
    expect(bc).toHaveTextContent("Up to date");
    expect(within(fresh).getByText("Mail").closest("tr")).toHaveTextContent("Behind");
    expect(screen.getByTestId("trust-answers-empty")).toHaveTextContent("Not measured yet");
    const auto = screen.getByTestId("trust-autopilot");
    const teams = within(auto).getByText("Teams chat").closest("tr")!;
    expect(teams).toHaveTextContent("Vince");
    expect(teams).toHaveTextContent("Draft");
    expect(teams).toHaveTextContent("88.2%");
    expect(teams).toHaveTextContent("Checked 12 of the 15 items it needs; 91.7% right so far, 90% needed");
    expect(within(auto).getByText(/question/i).closest("tr")).toHaveTextContent("not scored yet");
    // a rate with no whole count shows no invented count
    const rough = within(screen.getByTestId("trust-alerts")).getByText("Customer order later than this customer's orders usually are").closest("tr")!;
    expect(rough).toHaveTextContent("59");
    expect(rough).toHaveTextContent("21.0%");
    expect(rough).toHaveTextContent("–");
    expect(screen.getByText("2 types not measured yet (off until hand-checked)")).toBeInTheDocument();
    const sw = screen.getByTestId("trust-switches");
    expect(within(sw).getByText("Business Central").closest("tr")).toHaveTextContent("Off");
    expect(within(sw).getByText("Mission Control").closest("tr")).toHaveTextContent("Read only");
  });

  it("says each summary figure as a sentence, never a bare count, and a failed read's reason", async () => {
    const fresh: TrustFreshness[] = [
      { system: "BC", captured_at: "2026-11-11T18:00:00Z", served_at: "2026-11-11T18:00:00Z", lag_seconds: 7200, stale: false, capture: null, state_words: "Up to date: everything read is in the app." },
      { system: "Email", captured_at: "2026-11-11T18:30:00Z", served_at: "2026-11-11T18:26:00Z", lag_seconds: 600, stale: false, capture: null, state_words: "Up to date: the newest read joins the app's data within a few minutes." },
      { system: "File", captured_at: "2026-11-11T17:00:00Z", served_at: "2026-11-11T17:00:00Z", lag_seconds: 900, stale: false, capture: "failed", state_words: "Up to date: everything read is in the app. The last read of SharePoint files, at 1:16 PM ET, failed: it finished without reporting anything it read. The last good read was at 1:13 PM ET." },
      { system: "Teams", captured_at: "2026-11-11T18:30:00Z", served_at: "2026-11-11T16:00:00Z", lag_seconds: 300, stale: true, capture: null, state_words: "Behind: records read since 11:00 AM ET are not in the app yet; they normally arrive within a few minutes." },
    ];
    api.trust.mockResolvedValue(page({ freshness: fresh }));
    render(<TrustRoute />);
    const summary = await screen.findByTestId("trust-summary");
    const lines = within(summary).getAllByRole("listitem").map((li) => li.textContent);
    expect(lines).toEqual([
      "Teams is behind: records read since 11:00 AM ET are not in the app yet; they normally arrive within a few minutes.",
      expect.stringMatching(/^Files: The last read of SharePoint files/),
      "Answer accuracy on questions never used for tuning has not been measured on this server yet.",
      "1 of the 4 alert types is switched on; the rest stay off (1 was checked by hand but has not passed yet, 2 have not been checked yet).",
    ]);
    expect(screen.getByText(/prepares drafts for a person to confirm in 2 kinds of work/)).toBeInTheDocument();
    // no "6" beside "3 behind" that reads as "63 behind", no "0 of 0"
    expect(summary).not.toHaveTextContent(/\b0 of 0\b|\d+ behind/);
    const table = screen.getByTestId("trust-freshness");
    expect(within(table).getByText("Files").closest("tr")).toHaveTextContent("failed: it finished without reporting anything it read");
    const teams = within(table).getByText("Teams").closest("tr")!;
    // one verdict word, then the reason without repeating it
    expect(teams).toHaveTextContent("Behind");
    expect(teams).toHaveTextContent("records read since 11:00 AM ET");
    expect(teams.textContent).not.toMatch(/Behind.*Behind/);
    // an up-to-date source says only that
    const bc = within(table).getByText("Business Central").closest("tr")!;
    expect(bc.textContent).toMatch(/Up to date/);
    expect(bc.textContent).not.toMatch(/everything read/);
    expect(within(table).queryByText(/last capture failed/)).toBeNull();
  });

  it("lists every source Admin checks, each with one verdict, a source never read included", async () => {
    const fresh: TrustFreshness[] = [
      { system: "Portal", captured_at: "2026-11-11T19:50:00Z", served_at: "2026-11-11T19:50:00Z", lag_seconds: 600, stale: false, capture: null, state_words: "Up to date: everything read is in the app." },
      { system: "Sage", captured_at: "2026-11-11T19:40:00Z", served_at: null, lag_seconds: 1200, stale: false, capture: null, state_words: "Up to date: everything read is in the app." },
      { system: "FileMaker", captured_at: "2026-11-11T19:40:00Z", served_at: null, lag_seconds: 1200, stale: true, capture: null, state_words: "Behind: it reaches the app with the Portal data, which is behind." },
      { system: "Calendar", captured_at: "2026-11-11T19:30:00Z", served_at: null, lag_seconds: 1800, stale: false, capture: null, state_words: "Up to date: everything read is in the app." },
      { system: "Planner", captured_at: null, served_at: null, lag_seconds: null, stale: true, capture: null, state_words: "Never read yet." },
    ];
    api.trust.mockResolvedValue(page({ freshness: fresh }));
    render(<TrustRoute />);
    const table = await screen.findByTestId("trust-freshness");
    const verdict = (name: string) => within(table).getByText(name).closest("tr")!.children[1].textContent;
    expect(["Customer Portal", "Sage", "FileMaker", "Calendar", "Planner"].map(verdict)).toEqual(["Up to date", "Up to date", "Behind", "Up to date", "Not read yet"]);
    expect(within(table).getByText("Planner").closest("tr")!.textContent).not.toMatch(/Never read yet/);
    const att = screen.getByTestId("trust-summary");
    expect(att).toHaveTextContent("FileMaker is behind");
    expect(att).toHaveTextContent("Planner has not been read yet");
  });

  it("says when the newest scorer run was not counted, beside the older counts", async () => {
    const older = { judged: 12, matched: 11, need_judged: 15, need_percent: 90, percent: 91.7, passes: false, window: [], data_end: null, run: "run-a", refused: "status:partial", refused_run: "run-b" };
    api.trust.mockResolvedValue(page({ autopilot: [row({ countdown: older })] }));
    render(<TrustRoute />);
    const auto = await screen.findByTestId("trust-autopilot");
    expect(within(auto).getByText("Teams chat").closest("tr")).toHaveTextContent(
      "Checked 12 of the 15 items it needs; 91.7% right so far, 90% needed. The newest check was not counted (status:partial), so these are from an earlier one",
    );
  });

  it("words the gate from the server's rules, and says nothing acts when they are unreadable", async () => {
    api.trust.mockResolvedValue(page({ autopilot_gate: { need_judged: 20, need_percent: 95 } }));
    const { unmount } = render(<TrustRoute />);
    expect(await screen.findByText(/at least 20 held-out items judged \(fresh days, or past days never tuned on\), 95% or more right/)).toBeInTheDocument();
    expect(screen.getByText("Gate: held-out items at ≥95%")).toBeInTheDocument();
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
    expect(held).toHaveTextContent("9 of 10 right");   // the count leads, the percent follows it
    expect(held).toHaveTextContent("answered 10 of 12");
    expect(held).toHaveTextContent("12 questions, 12 runs");
    expect(held).toHaveTextContent("10 Nov");
  });

  it("a reader the server refuses is told they have no access, nothing of the page", async () => {
    api.trust.mockRejectedValue(new ApiError(404, "not_found"));
    render(<TrustRoute />);
    expect(await screen.findByTestId("trust-refused")).toHaveTextContent("You don't have access to this.");
    expect(screen.queryByText("Page not found")).not.toBeInTheDocument();
    expect(screen.queryByTestId("trust-summary")).not.toBeInTheDocument();
    expect(screen.queryByTestId("trust-freshness")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });
});
