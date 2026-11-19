import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AutopilotView } from "@/components/autopilot/area";
import { mailtoOf, scoreOf } from "@/components/autopilot/prepared";
import type { PreparedItem } from "@/lib/api";
import type { AutopilotArea } from "@/lib/types";

const nav = vi.hoisted(() => ({ params: "", replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(nav.params), useRouter: () => ({ push: vi.fn(), replace: nav.replace }) }));

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
  beforeEach(() => {
    vi.unstubAllGlobals();
    nav.params = "";
    nav.replace.mockReset();
  });
  afterEach(cleanup);

  it("a link to one item marks it, says when it is no longer waiting, and the old audit link opens the log", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [reply, card] }) });
    nav.params = "item=ap_2";
    render(<AutopilotView area={area()} />);
    await screen.findByText("Lee Supplier");
    expect(document.getElementById("prepared-ap_2")?.className).toContain("border-accent");
    expect(document.getElementById("prepared-ap_1")?.className).not.toContain("border-accent");
    expect(screen.queryByText(/no longer waiting/)).toBeNull();
    cleanup();
    nav.params = "item=ap_gone";
    render(<AutopilotView area={area()} />);
    expect(await screen.findByText(/no longer waiting for you/)).toBeTruthy();
    cleanup();
    nav.params = "tab=audit";
    render(<AutopilotView area={area()} />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/autopilot-log"));
  });

  it("a send the server refused stays on the card to confirm again", async () => {
    fetchMock({
      "/api/autopilot/drafts": () => ({ items: [reply] }),
      "/api/autopilot/items/ap_1/decision": () => ({ item_id: "ap_1", state: "waiting", decision: { decision: "accept", sent: false, outcome: "refused" } }),
    });
    render(<AutopilotView area={area()} />);
    await screen.findByText("Dana Buyer");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/Not sent: the send was refused or failed/)).toBeTruthy();
    expect(screen.getByText("Yes, Friday works.")).toBeTruthy();
  });

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

  it("a card asks one question with one-tap answers; a tap shows that answer's full reply and Confirm sends it", async () => {
    const options = [{ label: "Yes", reply: "Yes, 12.50 a case works." }, { label: "No, list", reply: "Sorry, we stay at list." }];
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [{ ...card, options }] }),
      "/api/autopilot/items/ap_2/decision": () => ({ item_id: "ap_2", decision: { decision: "accept", sent: false } }),
    });
    render(<AutopilotView area={area()} />);
    await screen.findByRole("button", { name: "No, list" });
    // the replies wait behind the answers: nothing is shown or decided before a tap
    expect(screen.queryByText("Sorry, we stay at list.")).toBeNull();
    expect(screen.getByLabelText("Other: answer in a few words")).toBeTruthy();
    expect(calls.some(([p]) => p.endsWith("/decision"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "No, list" }));
    expect(await screen.findByText("Sorry, we stay at list.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.some(([p, b]) => p === "/api/autopilot/items/ap_2/decision" && (b as { text?: string }).text === "Sorry, we stay at list.")).toBe(true));
  });

  it("a draft shows the preferences it applied and what an edit taught; a preference is saved from the card", async () => {
    const used = { memory: [{ id: 7, kind: "preference", text: "Never mention prices to Kim" }], related: [], template: null };
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [{ ...card, kind: "reply", body: "Kim, it ships Friday.", edited: true, learned: ["Starts replies with just the person's name"], used_for_draft: used }] }),
      "/api/memory": () => ({ id: 8, kind: "preference", text: "Always sign off V" }),
    });
    render(<AutopilotView area={area()} />);
    expect(await screen.findByText("Never mention prices to Kim")).toBeTruthy();
    expect(screen.getByText("Applied")).toBeTruthy();
    expect(screen.getByTestId("draft-learned").textContent).toContain("Starts replies with just the person's name");
    await userEvent.type(screen.getByLabelText(/Save as a preference/), "Always sign off V");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some(([p, b]) => p === "/api/memory" && (b as { kind: string; text: string }).kind === "preference" && (b as { text: string }).text === "Always sign off V")).toBe(true));
    expect(await screen.findByText(/Saved as your preference/)).toBeTruthy();
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

  it("a choice the owner's Draft file turned on shows on, set by the owner, and an empty list says it is on and when it last looked", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [] }) });
    const drafts = [
      { choice: "customer_replies", label: "Replies to customers", on: true, by_owner: true },
      { choice: "teams_replies", label: "Replies in Teams", on: true, by_owner: false, since: "2026-11-18T19:00:00Z" },
      { choice: "collections", label: "Collections and invoices", on: false },
      { choice: "approvals", label: "Approvals", on: true, partly: true, by_owner: true },
    ];
    render(<AutopilotView area={area({ my_drafts: drafts, checked_at: "2026-11-18T23:14:34Z" })} />);
    const states = (await screen.findAllByTestId("draft-choice-state")).map((e) => e.textContent);
    expect(states[0]).toBe("On, set by the owner");
    expect(states[1]).toMatch(/^On since /);
    expect(states[2]).toBe("Off");
    expect(states[3]).toBe("Partly on, set by the owner");
    expect(screen.getByRole("switch", { name: "Approvals: on" }).textContent).toBe("Turn off");
    expect(screen.getByRole("switch", { name: "Replies to customers: on" }).textContent).toBe("Turn off");
    expect((await screen.findByTestId("prepared-empty")).textContent).toMatch(/^On\. Nothing new for you as of /);
    cleanup();
    render(<AutopilotView area={area({ my_drafts: [{ ...drafts[2] }] })} />);
    expect((await screen.findByTestId("prepared-empty")).textContent).toBe("Nothing is prepared for you: nothing is switched on above.");
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
