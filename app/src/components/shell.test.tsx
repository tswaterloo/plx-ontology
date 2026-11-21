import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
vi.mock("./auth", () => ({
  useAuth: () => ({ state: auth.state }),
  RequireAuth: ({ children }: { children: unknown }) => children,
}));
const nav = vi.hoisted(() => ({ path: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));

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
import { PLACES } from "@/components/ui/glyphs";
import { AccessBanner, AdminAlarm, DataAsOf, DevBanner, NAV_SECTIONS, navFor, OpsAlarmBanner, pageSources, personNotice, SidebarNav, TOOLBAR_PAGES } from "./shell";

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

  it("an access-link sign-in says so, with no Demo or not-for-staff wording", () => {
    auth.state = { ...me({ sign_in: "access_link", data_label: "source-mirror captures" }), config: { mode: "dev", sign_in: "access_link" } };
    render(<Banners />);
    expect(screen.queryByTestId("dev-banner")).toBeNull();
    const link = screen.getByTestId("link-banner");
    expect(link).toHaveTextContent("Signed in with your access link");
    expect(document.body.innerHTML).not.toMatch(/demo|not for staff/i);
  });

  it("a demo server keeps the Demo line even when it says access_link for a fixture", () => {
    auth.state = me({ sign_in: "demo" });
    render(<Banners />);
    expect(screen.getByTestId("dev-banner")).toHaveTextContent("Demo sign-in");
    expect(screen.queryByTestId("link-banner")).toBeNull();
  });

  it("an unknown sign-in kind fails closed to the Demo line", () => {
    auth.state = me({ sign_in: "something_new" });
    render(<Banners />);
    expect(screen.getByTestId("dev-banner")).toHaveTextContent("Demo sign-in");
  });

  it("a server claiming Microsoft for a dev session still shows the Demo line", () => {
    auth.state = me({ sign_in: "microsoft" });
    render(<Banners />);
    expect(screen.getByTestId("dev-banner")).toBeInTheDocument();
  });

  it("the sign-in page says nothing about an access link before anyone signs in", () => {
    auth.state = { status: "signed-out", config: { mode: "dev", sign_in: "access_link" } };
    render(<Banners />);
    expect(screen.queryByTestId("link-banner")).toBeNull();
    expect(screen.queryByTestId("dev-banner")).toBeNull();
  });

  it("no grant, no label", () => {
    auth.state = { ...me({ auth: "entra", dev_mode: false }), config: { mode: "entra" } };
    render(<Banners />);
    expect(screen.queryByTestId("access-banner")).toBeNull();
    expect(screen.queryByTestId("dev-banner")).toBeNull();
  });
});

describe("data as of: one quiet line, amber only for a source the page reads", () => {
  afterEach(() => {
    nav.path = "/";
    auth.state = {};
    api.alarms = null;
    api.asked = 0;
  });
  const at = new Date().toISOString();
  const src = (system: string, age: number, extra: Record<string, unknown> = {}) => ({ system, as_of: at, age_seconds: age, stale: false, capture: "complete", ...extra });
  const home = (sources: unknown[]) => ({ freshness: { as_of: at, sources } }) as never;

  it("says only the time while every source the page reads is within the hour", () => {
    render(<DataAsOf home={home([src("Email", 600), src("BC", 1200)])} />);
    const line = screen.getByTestId("home-freshness");
    expect(line.textContent).toMatch(/^Data as of \d/);
    expect(line).not.toHaveAttribute("data-behind");
    expect(line.textContent).not.toMatch(/behind|ops|missed|not updating/i);
  });

  it("names a source the page reads that the server calls behind, and only that one", () => {
    nav.path = "/";
    render(<DataAsOf home={home([src("Email", 3 * 3600, { stale: true }), src("File", 9 * 3600, { stale: true }), src("BC", 60)])} />);
    const line = screen.getByTestId("home-freshness");
    expect(line).toHaveAttribute("data-behind");
    // Home reads mail, not SharePoint files: Files is not named on Home
    expect(line.textContent).toMatch(/· Mail behind$/);
  });

  it("a source older than an hour but within its system's bound is not behind (Trust says the same)", () => {
    nav.path = "/";
    render(<DataAsOf home={home([src("Email", 2 * 3600, { stale: false }), src("BC", 3 * 3600, { stale: false })])} />);
    expect(screen.getByTestId("home-freshness")).not.toHaveAttribute("data-behind");
  });

  it("without the server's verdict, more than an hour old is behind", () => {
    nav.path = "/";
    render(<DataAsOf home={home([src("Email", 2 * 3600, { stale: undefined })])} />);
    expect(screen.getByTestId("home-freshness")).toHaveAttribute("data-behind");
  });

  it("stays quiet on a page that reads none of the late sources", () => {
    nav.path = "/templates";
    render(<DataAsOf home={home([src("Email", 30 * 3600, { capture: "failed" })])} />);
    expect(screen.getByTestId("home-freshness")).not.toHaveAttribute("data-behind");
    expect(pageSources("/templates")).toEqual([]);
    expect(pageSources("/po/106093")).toContain("BC");
    expect(pageSources("/poland")).toEqual([]);
  });

  it("a failed or never-run capture is behind whatever its age; nothing before the data's time is known", () => {
    nav.path = "/"; // Home and its Late POs tab read Business Central
    const { unmount } = render(<DataAsOf home={home([src("BC", 10, { capture: "failed" })])} />);
    expect(screen.getByTestId("home-freshness").textContent).toMatch(/Business Central behind$/);
    unmount();
    render(<DataAsOf home={null} />);
    expect(screen.queryByTestId("home-freshness")).toBeNull();
  });

  it("puts the reliability banner on the Admin screens only, never on a person's page", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = { configured: true, running: true, checked_at: at, open: [{ key: "capture_failing:sharepoint", line: "", opened_at: at }], recovered: [], checks: [] };
    nav.path = "/";
    const { unmount } = render(<AdminAlarm />);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByTestId("ops-alarm-banner")).toBeNull();
    unmount();
    nav.path = "/ops";
    render(<AdminAlarm />);
    expect(await screen.findByTestId("ops-alarm-banner")).toHaveTextContent("SharePoint files are not updating");
  });

  it("marks the Admin item with a dot for an ops reader when a problem is open; nothing asked for anyone else", async () => {
    api.alarms = { configured: true, running: true, checked_at: at, open: [{ key: "delta_last_cycle", line: "", opened_at: at }], recovered: [], checks: [] };
    const { unmount } = render(<SidebarNav me={{ principal: "p", ops_reader: true } as never} late={null} />);
    expect(await screen.findByTestId("admin-dot")).toBeInTheDocument();
    expect(screen.queryByText(/ops targets? missed/)).toBeNull();
    unmount();
    api.asked = 0;
    render(<SidebarNav me={{ principal: "q" } as never} late={null} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByTestId("admin-dot")).toBeNull();
    expect(api.asked).toBe(0);
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
  const check = (name: string, value: number, unit = "h") => ({ name, value, target: 5, unit, state: "breach", detail: "", line: "" });
  const internals = [
    { key: "unit:plx-ingest.service", line: "plx-ingest.service (system): killed: out of memory", opened_at: "2030-01-02T16:40:00Z" },
    { key: "delta_apply_cost", line: "Data refresh apply cost 7.9 then 40.2 ms", opened_at: "2030-01-02T16:40:00Z" },
    { key: "pg_connections", line: "Postgres 91% of connections", opened_at: "2030-01-02T16:40:00Z" },
  ];

  it("says in one plain line only what changes what the person sees, linked to Admin", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = {
      ...alarms,
      open: [...internals, ...alarms.open, { key: "capture_newest:mail", line: "mail: newest 9:10 AM ET (2.2 h ago; target 1 h)", opened_at: "2030-01-02T16:40:00Z" }],
      checks: [check("capture_newest:mail", 2.2)],
    };
    render(<OpsAlarmBanner />);
    const banner = await screen.findByTestId("ops-alarm-banner");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveAttribute("href", "/ops");
    expect(banner.textContent).toBe("Mail is 2 hours behind. Records may be out of date. Alerts show the last check's findings; new ones follow when the check runs again.");
    // no unit names, numbers of milliseconds, internal keys or counts of "problems", in the line or its tooltip
    const all = `${banner.textContent} ${banner.getAttribute("title")}`;
    expect(all).not.toMatch(/plx-|\.service|\.timer|ms\b|Postgres|target|reliability problem|_/);
    expect(banner.querySelectorAll(".block")).toHaveLength(0);
    expect(banner.outerHTML).not.toMatch(/animate-|rounded-full/);
  });

  it("shows nothing for problems only the Admin page needs, nor when the checks or the poll fail", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = { ...alarms, open: internals };
    render(<OpsAlarmBanner every={30} />);
    await waitFor(() => expect(api.asked).toBeGreaterThan(0));
    api.alarms = { ...alarms, open: [], running: false };
    await waitFor(() => expect(api.asked).toBeGreaterThan(1));
    api.alarms = new Error("network");
    await waitFor(() => expect(api.asked).toBeGreaterThan(2));
    expect(screen.queryByTestId("ops-alarm-banner")).toBeNull();
  });

  it("names several behind sources in one sentence and says when one has stopped", async () => {
    const at = "2030-01-02T16:40:00Z";
    expect(personNotice({ ...alarms, open: [{ key: "capture_failing:teams", line: "", opened_at: at }] } as never)?.text).toBe("Teams messages are not updating.");
    expect(personNotice({
      ...alarms,
      open: ["mail", "teams", "bc"].map((s) => ({ key: `served_lag:${s}`, line: "", opened_at: at })),
      checks: [check("served_lag:mail", 0.5)],
    } as never)?.text).toBe("Mail, Teams messages and Business Central records are behind.");
    expect(personNotice({ ...alarms, open: [{ key: "capture:portal", line: "", opened_at: at }], checks: [check("capture:portal", 3 * 24 * 60, "min")] } as never)?.text)
      .toBe("Customer Portal records are 3 days behind.");
  });

  it("alerts or search catching up is a calm line; a source behind stays the red one", async () => {
    const at = "2030-01-02T16:40:00Z";
    const mending = personNotice({ ...alarms, open: [{ key: "proactive_last_pass", line: "", opened_at: at }, { key: "search_follow_lag", line: "", opened_at: at }] } as never);
    expect(mending).toEqual({ text: "Alerts show the last check's findings; new ones follow when the check runs again. Search may not find the newest items yet; it catches up on its own.", calm: true });
    expect(personNotice({ ...alarms, open: [{ key: "proactive_last_pass", line: "", opened_at: at }, { key: "capture_failing:teams", line: "", opened_at: at }] } as never)?.calm).toBe(false);
    expect(personNotice({ ...alarms, open: [{ key: "delta_last_cycle", line: "", opened_at: at }] } as never)?.calm).toBe(false);
  });

  it("planned maintenance is one calm line; an overrun says so plainly", async () => {
    auth.state = me({ ops_reader: true });
    api.alarms = { ...alarms, open: [], maintenance: { state: "in_window", reason: "Postgres migration train 4", until: "2030-01-02T19:30:00Z" } };
    render(<OpsAlarmBanner />);
    const banner = await screen.findByTestId("ops-alarm-banner");
    expect(banner).toHaveAttribute("role", "status");
    expect(banner.textContent).toMatch(/^Planned maintenance until .*2:30 PM ET: alerts and updates are paused\.$/);
    expect(banner).not.toHaveTextContent("Postgres");
    expect(banner.querySelector("svg")).toBeNull();
    expect(personNotice({ ...alarms, open: [], maintenance: { state: "overrun", until: "2030-01-02T19:30:00Z" } } as never)?.text)
      .toBe("Maintenance is taking longer than planned.");
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

  it("a refused poll (403) says nothing and is not asked again", async () => {
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
    // and the list names no page that does not exist (it once held "/so")
    expect(TOOLBAR_PAGES.filter((p) => !routes.includes(p))).toEqual([]);
  });
});

describe("sidebar icons", () => {
  it("every sidebar place has its icon", () => {
    for (const s of NAV_SECTIONS) expect(PLACES[s.place], s.label).toBeTruthy();
  });
});
