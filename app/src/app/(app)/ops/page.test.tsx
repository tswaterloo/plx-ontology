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
});
