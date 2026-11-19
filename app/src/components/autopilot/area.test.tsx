import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AutopilotView } from "@/components/autopilot/area";
import { mailtoOf, scoreOf } from "@/components/autopilot/prepared";
import type { PreparedItem } from "@/lib/api";
import type { AutopilotArea } from "@/lib/types";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(), useRouter: () => ({ push: vi.fn() }) }));

const reply: PreparedItem = { item_id: "ap_1", sender: "Dana Buyer", channel: "mail", subject: "RE: PO 7", message: "Can you ship Friday?", kind: "reply", to: ["dana@customer.example"], cc: [], body: "Yes, Friday works.", edited: false, ask: "", facts: [], created_at: "2026-11-18T14:00:00Z" };
const card: PreparedItem = { ...reply, item_id: "ap_2", sender: "Lee Supplier", message: "Can you do 12.50 a case?", kind: "your_call", body: "", ask: "Do you agree to 12.50 a case?", facts: ["Their PO P-1."] };

const area = (extra: Record<string, unknown> = {}) =>
  ({ owner: false, switches: { global_off: false, autopilot_off: false, master_switch: false, workflows_off: [] }, my_drafts: [], people: [], items: [], workflows: [], my_opt_ins: [], roles: [], known_workflows: [], ...extra }) as unknown as AutopilotArea;

function fetchMock(handlers: Partial<Record<string, (body: unknown) => unknown>>) {
  const calls: [string, unknown][] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url, "http://x").pathname;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push([path, body]);
    const h = handlers[path];
    return new Response(JSON.stringify(h ? h(body) : {}), { status: h ? 200 : 404, headers: { "content-type": "application/json" } });
  }));
  return calls;
}

describe("the autopilot page", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(cleanup);

  it("lists what was prepared with Edit, Confirm, Copy and Open in Outlook; a card asks for your line; no Workflows, Builder or Schedules", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [reply, card] }) });
    render(<AutopilotView area={area()} />);
    expect(await screen.findByText("Dana Buyer")).toBeTruthy();
    expect(screen.getByText("Can you ship Friday?")).toBeTruthy();
    expect(screen.getByText("Yes, Friday works.")).toBeTruthy();
    for (const name of ["Confirm", "Edit", "Copy"]) expect(screen.getAllByRole("button", { name }).length).toBe(1);
    const outlook = screen.getByRole("link", { name: "Open in Outlook" });
    expect(outlook.getAttribute("href")).toBe("mailto:dana%40customer.example?subject=RE%3A%20PO%207&body=Yes%2C%20Friday%20works.");
    expect(screen.getByText("Do you agree to 12.50 a case?")).toBeTruthy();
    expect(screen.getByLabelText("What do you want to say?")).toBeTruthy();
    for (const gone of ["Workflows", "Builder", "Schedules", "Review queue"]) expect(screen.queryByRole("tab", { name: gone })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Everyone" })).toBeNull();
  });

  it("Confirm accepts the item, Copy puts the reply on the clipboard, and a line on a card goes to reply-from-line", async () => {
    const write = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText: write } });
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [reply, card] }),
      "/api/autopilot/items/ap_1/decision": () => ({ item_id: "ap_1", decision: { decision: "accept", sent: false } }),
      "/api/autopilot/items/ap_2/reply-from-line": () => ({ item_id: "ap_2", decision: { decision: "edit", text: "Yes, 12.50 works." } }),
    });
    render(<AutopilotView area={area()} />);
    await screen.findByText("Dana Buyer");
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(write).toHaveBeenCalledWith("Yes, Friday works.");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.some(([p, b]) => p === "/api/autopilot/items/ap_1/decision" && (b as { decision: string }).decision === "accept")).toBe(true));
    expect(await screen.findByText(/Nothing was sent/)).toBeTruthy();
    await userEvent.type(screen.getByLabelText("What do you want to say?"), "yes at 12.50");
    await userEvent.click(screen.getByRole("button", { name: "Write the reply" }));
    await waitFor(() => expect(calls.some(([p, b]) => p.endsWith("/reply-from-line") && (b as { line: string }).line === "yes at 12.50")).toBe(true));
  });

  it("admins get an Everyone tab: who switched what on and each workflow's score, flagged below the bar", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [] }) });
    render(
      <AutopilotView
        area={area({ owner: true, people: [{ id: "p1", name: "Ana", you: false, roles: [] }], drafts_switched_on: [{ person: "p1", choice: "mail_replies", label: "Replies to mail" }], outcomes: { by_autopilot: { customer_question: { accepted: 1, edited: 1, rejected: 10 }, task_assigned: { accepted: 0, edited: 0, rejected: 0 } } } })}
      />,
    );
    await userEvent.click(await screen.findByRole("tab", { name: "Everyone" }));
    expect(screen.getByText("Ana")).toBeTruthy();
    expect(screen.getByText("Replies to mail")).toBeTruthy();
    expect(screen.getByText(/1 accepted, 1 edited, 10 rejected/)).toBeTruthy();
    expect(screen.getByText(/keep it off: 2 of 12 kept/)).toBeTruthy();
    expect(screen.queryByText(/0 accepted, 0 edited/)).toBeNull(); // no zero counters
  });

  it("scoreOf flags a workflow only with ten settled and under a fifth kept", () => {
    expect(scoreOf({ accepted: 1, edited: 0, rejected: 9 }).off).toBe(true);
    expect(scoreOf({ accepted: 2, edited: 0, rejected: 8 }).off).toBe(false); // exactly a fifth
    expect(scoreOf({ accepted: 0, edited: 0, rejected: 9 }).off).toBe(false); // too few
  });

  it("mailto carries cc, subject and body", () => {
    expect(mailtoOf({ ...reply, cc: ["a@x.co"] }, "Hi")).toBe("mailto:dana%40customer.example?cc=a%40x.co&subject=RE%3A%20PO%207&body=Hi");
  });
});
