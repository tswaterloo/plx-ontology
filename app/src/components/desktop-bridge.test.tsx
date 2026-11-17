import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
vi.mock("./auth", () => ({ useAuth: () => ({ state: auth.state }) }));
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
const open = vi.fn();
vi.mock("./feedback-dialog", () => ({ useFeedbackDialog: () => ({ open }) }));
const api = vi.hoisted(() => ({ items: [] as unknown[], alarms: [] as unknown[], memory: [] as unknown[] }));
vi.mock("@/lib/api", () => ({
  getAutopilot: () => Promise.resolve({ items: api.items }),
  getOpsAlarms: () => Promise.resolve({ open: api.alarms }),
  getMemory: () => Promise.resolve({ available: true, items: api.memory }),
}));

import { DesktopBridge } from "./desktop-bridge";

const signedIn = (extra: Record<string, unknown> = {}) => ({ status: "signed-in", me: { principal: "p1", ...extra } });
const item = (id: string, over: Record<string, unknown> = {}) => ({ item_id: id, title: `Item ${id}`, mine: true, state: "waiting", ...over });

function fakeDesktop() {
  let menu: ((m: unknown) => void) | null = null;
  const notify = vi.fn();
  (window as unknown as { plxDesktop: unknown }).plxDesktop = {
    app: true, version: "0.1.0", notify, onMenu: (cb: (m: unknown) => void) => ((menu = cb), () => (menu = null)),
  };
  return { notify, send: (m: unknown) => menu?.(m) };
}
afterEach(() => {
  api.memory = [];
  window.localStorage.clear();
  delete (window as unknown as { plxDesktop?: unknown }).plxDesktop;
  vi.useRealTimers();
  push.mockReset();
  open.mockReset();
});

describe("DesktopBridge", () => {
  it("does nothing in a browser", async () => {
    auth.state = signedIn();
    const { container } = render(<DesktopBridge />);
    expect(container.innerHTML).toBe("");
  });

  it("routes menu choices to the feedback sheet and to pages", () => {
    auth.state = signedIn();
    const d = fakeDesktop();
    render(<DesktopBridge />);
    act(() => d.send({ action: "feedback" }));
    expect(open).toHaveBeenCalledTimes(1);
    act(() => d.send({ action: "ask" }));
    expect(push).toHaveBeenCalledWith("/ask");
    act(() => d.send({ href: "https://evil.example/" }));
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("notifies for a newly waiting item and a new alarm, not for what was there when the app opened", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    auth.state = signedIn({ ops_reader: true });
    api.items = [item("old")];
    api.alarms = [{ key: "unit:x", line: "plx-x failed" }];
    const d = fakeDesktop();
    render(<DesktopBridge />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(d.notify).not.toHaveBeenCalled();
    api.items = [item("old"), item("new"), item("someone-else", { mine: false }), item("decided", { state: "done" })];
    api.alarms = [{ key: "unit:x", line: "plx-x failed" }, { key: "disk", line: "Disk 95% full" }];
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    const told = d.notify.mock.calls.map((c) => c[0]);
    expect(told.map((n) => n.body)).toEqual(["Item new", "Disk 95% full"]);
    expect(told[0].href).toBe("/autopilot?item=new");
    // told once: the next read says nothing more about them
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(d.notify).toHaveBeenCalledTimes(2);
  });

  it("a reader who is not an ops reader is never sent alarms", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    auth.state = signedIn();
    api.items = [];
    api.alarms = [{ key: "disk", line: "Disk 95% full" }];
    const d = fakeDesktop();
    render(<DesktopBridge />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(d.notify).not.toHaveBeenCalled();
  });

  it("reminds of the reader's own overdue commitment when the app opens, once", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    auth.state = signedIn();
    api.items = [];
    api.memory = [{ id: 4, kind: "commitment", source: "ask", at: "2026-11-01T15:00:00Z", text: "Remind me to call Northwind", records: [], importance: 0.85, confidence: 0.8, due: "2026-11-02" }];
    const d = fakeDesktop();
    render(<DesktopBridge />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(d.notify.mock.calls.map((c) => c[0])).toEqual([{ title: "Overdue: you said you would", body: "Call Northwind", href: "/" }]);
    await act(async () => { await vi.advanceTimersByTimeAsync(11 * 60_000); });
    expect(d.notify).toHaveBeenCalledTimes(1);
  });
});
