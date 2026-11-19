import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { OpsAlarms } from "@/lib/api";

const api = vi.hoisted(() => ({ body: null as unknown }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getOpsAlarms: () => Promise.resolve(api.body),
}));

import OpsPage from "./page";

const base: OpsAlarms = { configured: true, running: true, checked_at: new Date().toISOString(), open: [], recovered: [], checks: [] };

describe("Reliability: job queue", () => {
  it("shows what runs, what waits and why, and dead letters", async () => {
    api.body = {
      ...base,
      queue: {
        configured: true, fresh: true, written_at: new Date().toISOString(), queued: 1,
        last_hour: { refused: 4, dead: 1 },
        memory: { available_mb: 9216, promised_mb: 4096, floor_mb: 4096, headroom_mb: 1024 },
        running: [{ id: 1, class: "delta", state: "running", mem_mb: 24576, current_mb: 6144, running_s: 600, waited_s: 3 }],
        waiting: [{ id: 2, class: "tests", waiting_s: 90, refusals: 4, last_refusal: "memory: 9216 MB available" }],
        dead: [{ id: 3, class: "search-follow", attempts: 3, last_error: "plx-search-follow.service: exit-code" }],
        unmanaged: [],
        events: [{ topic: "autopilot", state: "ready", n: 12, oldest_s: 300 }, { topic: "autopilot", state: "dead", n: 1 }],
      },
    };
    render(<OpsPage />);
    const queue = await screen.findByTestId("ops-queue");
    expect(queue).toHaveTextContent("1 queued · 1 running · 4 refusals and 1 dead in the last hour");
    expect(queue).toHaveTextContent("6.0 GB of 24.0 GB");
    expect(queue).toHaveTextContent("4 refused");
    expect(queue).toHaveTextContent("memory: 9216 MB available");
    expect(queue).toHaveTextContent("gave up after 3 tries");
    expect(screen.getByTestId("ops-queue-events")).toHaveTextContent("autopilot 12 waiting (oldest 5 min) · autopilot 1 dead");
    expect(screen.queryByTestId("ops-queue-stale")).toBeNull();
  });

  it("says when the queue worker stopped, and shows nothing before the queue is on", async () => {
    api.body = { ...base, queue: { configured: true, fresh: false, written_at: new Date(0).toISOString() } };
    const { unmount } = render(<OpsPage />);
    expect(await screen.findByTestId("ops-queue-stale")).toHaveTextContent("The queue worker has stopped");
    unmount();
    api.body = { ...base, queue: { configured: false, fresh: false } };
    render(<OpsPage />);
    await screen.findByTestId("ops-summary");
    expect(screen.queryByTestId("ops-queue")).toBeNull();
  });

  it("counts the open problems by area and names the oldest", async () => {
    const at = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();
    api.body = {
      ...base,
      open: [
        { key: "unit:plx-suite.service", line: "plx-suite.service (system): failed", opened_at: at(5) },
        { key: "capture:mail", line: "mail capture last refreshed 3 days ago", opened_at: at(50) },
        { key: "unit:plx-caddy.service", line: "plx-caddy.service (system): failed", opened_at: at(1) },
      ],
    } as OpsAlarms;
    render(<OpsPage />);
    const line = await screen.findByTestId("ops-by-area");
    expect(line).toHaveTextContent("Open by areaServices 2Data freshness 1");
    expect(line).toHaveTextContent("Oldest open 2 days: mail capture last refreshed 3 days ago");
  });
});

describe("Reliability: pauses and mail", () => {
  it("lists what is paused on purpose apart from the open problems, and says whether problems are mailed", async () => {
    api.body = {
      ...base,
      open: [{ key: "backup", line: "Nightly backup: newest complete backup is 50 h old", opened_at: new Date().toISOString(), level: "critical" }],
      paused: [{ key: "unit:plx-delta.timer", line: "plx-delta.timer (system): stopped: no next run", opened_at: new Date().toISOString(),
                 paused: { until: new Date(Date.now() + 3600e3).toISOString(), why: "migration window" } }],
      notify: { sending: false, why_not: "no sender set", recipients: 0, pending: 0 },
    };
    render(<OpsPage />);
    const paused = await screen.findByTestId("ops-paused");
    expect(paused).toHaveTextContent("plx-delta.timer (system): stopped: no next run");
    expect(paused).toHaveTextContent("migration window");
    expect(screen.getByTestId("ops-open")).not.toHaveTextContent("plx-delta.timer");
    expect(screen.getByTestId("ops-mail")).toHaveTextContent("Problems are not mailed (no sender set): they show only here.");
    expect(screen.queryByTestId("ops-maintenance")).toBeNull();
  });

  it("says a planned maintenance window is on and until when, and not once it has overrun", async () => {
    const until = new Date(Date.now() + 3600e3).toISOString();
    api.body = {
      ...base,
      paused: [{ key: "unit:plx-proactive.timer", line: "plx-proactive.timer (system): stopped: no next run", opened_at: new Date().toISOString(),
                 paused: { until, why: "maintenance: live data rebuild" } }],
      maintenance: { state: "in_window", reason: "live data rebuild", until },
    };
    const { unmount } = render(<OpsPage />);
    const note = await screen.findByTestId("ops-maintenance");
    expect(note).toHaveTextContent("Paused for maintenance until");
    expect(note).toHaveTextContent("live data rebuild");
    expect(screen.getByTestId("ops-paused")).toHaveTextContent("plx-proactive.timer");
    unmount();
    api.body = { ...base, open: [{ key: "maintenance_window", line: "Planned maintenance: live data rebuild: past its planned end", opened_at: new Date().toISOString(), level: "high" }],
                 maintenance: { state: "overrun", reason: "live data rebuild", until } };
    render(<OpsPage />);
    expect(await screen.findByTestId("ops-open")).toHaveTextContent("past its planned end");
    expect(screen.queryByTestId("ops-maintenance")).toBeNull();
  });
});
