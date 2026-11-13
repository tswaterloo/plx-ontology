import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
vi.mock("./auth", () => ({
  useAuth: () => ({ state: auth.state }),
  RequireAuth: ({ children }: { children: unknown }) => children,
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ push: () => undefined }) }));

const api = vi.hoisted(() => ({ ops: null as unknown, alarms: null as unknown, asked: 0 }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getOpsHealth: () => Promise.resolve(api.ops),
  getOpsAlarms: () => {
    api.asked += 1;
    return api.alarms instanceof Error ? Promise.reject(api.alarms) : Promise.resolve(api.alarms);
  },
}));

import { readdirSync } from "node:fs";
import { ApiError } from "@/lib/errors";
import { join } from "node:path";
import { AccessBanner, DevBanner, navFor, OpsAlarmBanner, OpsStatus, TOOLBAR_PAGES } from "./shell";

const me = (over: Record<string, unknown>) => ({
  status: "signed-in",
  config: { mode: "dev" },
  me: { principal: "p", display_name: "Vince", auth: "dev", dev_mode: true, data_label: "FIXTURE", access_label: null, ...over },
});

function Banners() {
  return (
    <>
      <DevBanner />
      <AccessBanner />
    </>
  );
}

describe("page banners", () => {
  afterEach(() => (auth.state = {}));

  it("DEV mode is one short line, the full-read label said once beside it", () => {
    auth.state = me({ access_label: "Viewing with full-read access" });
    render(<Banners />);
    const dev = screen.getByTestId("dev-banner");
    expect(dev).toHaveTextContent("Demo sign-in");
    expect(dev).not.toHaveTextContent("Not for staff use");
    expect(dev).toHaveAttribute("title", expect.stringContaining("FIXTURE"));
    expect(screen.getAllByTestId("access-banner")).toHaveLength(1);
    expect(screen.getAllByText("Viewing with full-read access")).toHaveLength(1);
  });

  it("Microsoft sign-in shows the full-read label once, and no DEV line", () => {
    auth.state = { ...me({ access_label: "Viewing with full-read access", auth: "entra", dev_mode: false }), config: { mode: "entra" } };
    render(<Banners />);
    expect(screen.queryByTestId("dev-banner")).toBeNull();
    expect(screen.getAllByTestId("access-banner")).toHaveLength(1);
    expect(screen.getAllByText("Viewing with full-read access")).toHaveLength(1);
  });

  it("no grant, no label", () => {
    auth.state = { ...me({ auth: "entra", dev_mode: false }), config: { mode: "entra" } };
    render(<Banners />);
    expect(screen.queryByTestId("access-banner")).toBeNull();
    expect(screen.queryByTestId("dev-banner")).toBeNull();
  });
});

describe("ops status line", () => {
  afterEach(() => {
    auth.state = {};
    api.ops = null;
  });
  const breach = {
    status: "breach",
    checked_at: "2030-01-02T12:00:00Z",
    breaches: ["delta_last_cycle", "truth_contradicted_share"],
    checks: [
      { name: "delta_last_cycle", value: 107, target: 15, unit: "min", state: "breach", detail: "" },
      { name: "truth_contradicted_share", value: 0.75, target: 0.25, unit: "share", state: "breach", detail: "" },
      { name: "capture:mail", value: 30, target: 360, unit: "min", state: "ok", detail: "" },
    ],
  };

  it("names each missed target to an ops reader", async () => {
    auth.state = me({ ops_reader: true });
    api.ops = breach;
    render(<OpsStatus />);
    const line = await screen.findByTestId("ops-status");
    expect(line).toHaveTextContent("Ops: 2 behind");
    expect(line).toHaveTextContent("Last update cycle, Alerts contradicted");
    expect(line).not.toHaveTextContent("Capture");
  });

  it("names a capture by its system and says long delays in hours or days", async () => {
    auth.state = me({ ops_reader: true });
    api.ops = {
      ...breach,
      breaches: ["capture:sharepoint_lists", "delta_last_cycle"],
      checks: [
        { name: "capture:sharepoint_lists", value: 12865.4, target: 160, unit: "min", state: "breach", detail: "" },
        { name: "delta_last_cycle", value: 2877.6, target: 15, unit: "min", state: "breach", detail: "" },
      ],
    };
    render(<OpsStatus />);
    const line = await screen.findByTestId("ops-status");
    expect(line).toHaveTextContent("SharePoint lists capture, Last update cycle");
    expect(line).not.toHaveTextContent("sharepoint_lists");
    await userEvent.click(line);
    const detail = await screen.findByTestId("ops-detail");
    expect(detail).toHaveTextContent("9 days (target 3 h)");
    expect(detail).toHaveTextContent("2 days (target 15 min)");
    expect(detail).not.toHaveTextContent("12865");
  });

  it("says nothing to other readers, or when every target holds", async () => {
    auth.state = me({});
    api.ops = breach;
    render(<OpsStatus />);
    auth.state = me({ ops_reader: true });
    api.ops = { ...breach, status: "ok", breaches: [], checks: [] };
    render(<OpsStatus />);
    await waitFor(() => expect(screen.queryByTestId("ops-status")).toBeNull());
  });
});

describe("reliability banner on every page", () => {
  afterEach(() => {
    auth.state = {};
    api.alarms = null;
    api.asked = 0;
  });
  const alarms = {
    configured: true,
    running: true,
    checked_at: "2030-01-02T17:00:00Z",
    open: [
      { key: "unit:plx-proactive.service", line: "plx-proactive.service (system): killed: out of memory", opened_at: "2030-01-02T16:40:00Z" },
      { key: "delta_last_cycle", line: "Data refresh (delta) last finished 3 days ago (target 15 min)", opened_at: "2030-01-02T16:40:00Z" },
    ],
    recovered: [],
    checks: [],
  };

  it("shows an ops reader every open problem in red, linked to /ops", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = alarms;
    render(<OpsAlarmBanner />);
    const banner = await screen.findByTestId("ops-alarm-banner");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent("2 reliability problems");
    expect(banner).toHaveTextContent("killed: out of memory");
    expect(banner).toHaveAttribute("href", "/ops");
    // one calm line: no second row, nothing that blinks or pulses
    expect(banner.querySelectorAll(".block")).toHaveLength(0);
    expect(banner.outerHTML).not.toMatch(/animate-|rounded-full/);
  });

  it("keeps the count, each problem's headline and the failed-poll notice in view of one line", async () => {
    auth.state = me({ ops_reader: true });
    const long = (n: number) => ({ key: `k${n}`, line: `mail capture last refreshed ${n} days ago (target 5 h); stale since Nov 10; last error child_timeout`, opened_at: "2030-01-02T16:40:00Z" });
    api.alarms = { ...alarms, open: [long(1), long(2), long(3), long(4)] };
    render(<OpsAlarmBanner every={30} />);
    const banner = await screen.findByTestId("ops-alarm-banner");
    expect(banner).toHaveTextContent("4 reliability problems: mail capture last refreshed 1 days ago (target 5 h) · mail capture last refreshed 2 days ago (target 5 h) · 2 more");
    expect(banner).not.toHaveTextContent("child_timeout");
    // the detail is not lost: every line in full in the tooltip
    expect(banner.getAttribute("title")).toContain("4 days ago (target 5 h); stale since Nov 10; last error child_timeout");
    api.alarms = new Error("network");
    await waitFor(() => expect(screen.getByTestId("ops-alarm-banner").textContent).toMatch(/^Monitoring unavailable/));
  });

  it("says when the checks themselves stopped", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = { ...alarms, running: false, open: [] };
    render(<OpsAlarmBanner />);
    expect(await screen.findByTestId("ops-alarm-banner")).toHaveTextContent("Reliability checks are not running");
  });

  it("keeps the last alarms on screen and says monitoring is unavailable when the next poll fails", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = alarms;
    render(<OpsAlarmBanner every={30} />);
    const banner = await screen.findByTestId("ops-alarm-banner");
    expect(banner).toHaveTextContent("2 reliability problems");
    expect(banner).not.toHaveTextContent("Monitoring unavailable");
    api.alarms = new Error("network");
    await waitFor(() => expect(screen.getByTestId("ops-alarm-banner")).toHaveTextContent("Monitoring unavailable"));
    expect(screen.getByTestId("ops-alarm-banner")).toHaveTextContent("killed: out of memory");
    // a good poll clears the warning
    api.alarms = alarms;
    await waitFor(() => expect(screen.getByTestId("ops-alarm-banner")).not.toHaveTextContent("Monitoring unavailable"));
  });

  it("says monitoring is unavailable when the very first poll fails", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = new Error("network");
    render(<OpsAlarmBanner />);
    expect(await screen.findByTestId("ops-alarm-banner")).toHaveTextContent("Monitoring unavailable");
  });

  it("an all-clear banner that loses its poll no longer looks like normal operation", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = { ...alarms, open: [] };
    render(<OpsAlarmBanner every={30} />);
    await waitFor(() => expect(api.asked).toBeGreaterThan(0));
    expect(screen.queryByTestId("ops-alarm-banner")).toBeNull();
    api.alarms = new Error("network");
    expect(await screen.findByTestId("ops-alarm-banner")).toHaveTextContent("Monitoring unavailable");
  });

  it("is never asked for by other readers and says nothing when all is well or refused", async () => {
    auth.state = me({});
    api.alarms = alarms;
    render(<OpsAlarmBanner />);
    // a full-read label alone is not the server's ops decision
    auth.state = me({ access_label: "Viewing with full-read access" });
    api.alarms = alarms;
    render(<OpsAlarmBanner />);
    expect(api.asked).toBe(0);
    auth.state = me({ ops_reader: true });
    api.alarms = { ...alarms, open: [] };
    render(<OpsAlarmBanner />);
    await waitFor(() => expect(api.asked).toBe(1));
    expect(screen.queryByTestId("ops-alarm-banner")).toBeNull();
  });

  it("a refused poll (403) says nothing and is not asked again; only a failed read says unavailable", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = new ApiError(403, "forbidden");
    render(<OpsAlarmBanner every={20} />);
    await waitFor(() => expect(api.asked).toBe(1));
    await new Promise((r) => setTimeout(r, 120)); // several intervals would have passed
    expect(api.asked).toBe(1);
    expect(screen.queryByTestId("ops-alarm-banner")).toBeNull();
  });
});

describe("reliability in the nav", () => {
  it("shows to ops readers only, whatever other grant a reader holds", () => {
    const hrefs = (m: Record<string, unknown>) => navFor(m).map((n) => n.href);
    expect(hrefs({ ops_reader: true })).toContain("/ops");
    expect(hrefs({ access_label: "Viewing with full-read access" })).not.toContain("/ops");
    expect(hrefs({ feedback_admin: true })).not.toContain("/ops");
    expect(hrefs({})).not.toContain("/ops");
  });
});

describe("phone title bar", () => {
  it("every page draws its own toolbar, so the shell never stacks a second bar over one", () => {
    const routes = readdirSync(join(__dirname, "../app/(app)"), { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => `/${d.name}`);
    expect(routes.length).toBeGreaterThan(10);
    expect(routes.filter((r) => !TOOLBAR_PAGES.includes(r))).toEqual([]);
  });
});
