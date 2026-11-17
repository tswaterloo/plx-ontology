import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ rows: null as unknown[] | null }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getActionsLog: () => (api.rows === null ? new Promise(() => undefined) : Promise.resolve({ rows: api.rows })),
}));

import ActivityPage from "./page";

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();
const row = (seq: number, kind: string, status: string, at: string, to: string[] = [], card = "PO 90412") => ({
  seq, kind, status, at, to, cc: [], card_id: `c${seq}`, card_label: card, text: "x", actor: "Ana", payload_hash: "h", missing: [], citations: [],
});

describe("Activity", () => {
  it("says what it is reading in words", () => {
    api.rows = null;
    render(<ActivityPage />);
    expect(screen.getByTestId("activity-pending")).toHaveTextContent("Reading your activity");
  });

  it("sums the last seven days and lists what needs a look", async () => {
    api.rows = [
      row(1, "send_email", "sent", ago(1), ["a@x.example", "B@x.example"]),
      row(2, "reply_in_thread", "sent", ago(2), ["b@x.example"], "SO 77120"),
      row(3, "send_email", "dry_run", ago(3), ["c@x.example"]),
      row(4, "create_follow_up", "follow_up", ago(4)),
      row(5, "dismiss", "dismissed", ago(5)),
      row(6, "send_email", "failed", ago(6), ["d@x.example"]),
      row(7, "send_email", "sent", ago(24 * 9), ["old@x.example"], "PO 1"),
    ];
    render(<ActivityPage />);
    const summary = await screen.findByTestId("activity-summary");
    const figure = (label: string) => [...summary.querySelectorAll("dt")].find((d) => d.textContent === label)?.nextElementSibling?.textContent;
    expect(figure("Sent")).toBe("2");
    expect(figure("Not sent (sending off)")).toBe("1");
    expect(figure("Tasks set or done")).toBe("1");
    expect(figure("Put away")).toBe("1");
    expect(figure("People reached")).toBe("4");
    expect(figure("Records")).toBe("2");
    expect(screen.getByTestId("activity-check")).toHaveTextContent("it failed; send it again from the record");
  });

  it("explains an empty journal in plain rows, no placeholder cards", async () => {
    api.rows = [];
    render(<ActivityPage />);
    const empty = await screen.findByTestId("activity-empty");
    expect(empty).toHaveTextContent("Nothing sent, approved or logged yet.");
    expect(empty.outerHTML).not.toMatch(/shadow-lift|rounded-full/);
  });

  it("filters the journal by what happened and names the records worked on most", async () => {
    api.rows = [
      row(1, "send_email", "sent", ago(1), ["a@x.example"]),
      row(2, "reply_in_thread", "sent", ago(2), ["b@x.example"]),
      row(3, "send_email", "dry_run", ago(3), ["c@x.example"], "SO 77120"),
      { ...row(4, "send_email", "unknown", ago(4), ["d@x.example"], "SO 77120"), missing: ["supplier contact"] },
      row(5, "snooze", "snoozed", ago(5)),
    ];
    render(<ActivityPage />);
    expect(await screen.findByTestId("activity-busiest")).toHaveTextContent("Most worked on: PO 90412 3 · SO 77120 2");
    expect(screen.getByRole("tab", { name: /^All\s*5$/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: /^Tasks/ })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /^Needs a look\s*1$/ }));
    const shown = screen.getAllByTestId("actions-log-row");
    expect(shown).toHaveLength(1);
    expect(shown[0]).toHaveTextContent("1 not on record");
    fireEvent.click(screen.getByRole("tab", { name: /^Sent\s*2$/ }));
    expect(screen.getAllByTestId("actions-log-row")).toHaveLength(2);
  });
});
