import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AutopilotView } from "@/components/autopilot/area";
import { AssistQueue } from "@/components/home/assist-queue";
import { mailtoOf, scoreOf, tomorrowMorning } from "@/components/autopilot/prepared";
import { forgetFeeds } from "@/lib/feeds";
import { optInScore } from "@/lib/autopilot";
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

const more = async (n = 0) => userEvent.click(screen.getAllByRole("button", { name: "More" })[n]);

describe("the queue on Home and its Switches tab", () => {
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
    render(<AssistQueue />);
    await screen.findByText("Lee Supplier");
    expect(document.getElementById("prepared-ap_2")?.className).toContain("border-accent");
    expect(document.getElementById("prepared-ap_1")?.className).not.toContain("border-accent");
    expect(screen.queryByText(/no longer waiting/)).toBeNull();
    cleanup();
    nav.params = "item=ap_gone";
    render(<AssistQueue />);
    expect(await screen.findByText(/no longer waiting for you/)).toBeTruthy();
    cleanup();
    nav.params = "tab=audit";
    render(<AutopilotView area={area()} />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/autopilot-log"));
    cleanup();
    nav.params = "item=ap_2";
    render(<AutopilotView area={area()} />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/?item=ap_2"));
  });

  it("a send the server refused stays on the card to confirm again", async () => {
    fetchMock({
      "/api/autopilot/drafts": () => ({ items: [reply] }),
      "/api/autopilot/items/ap_1/decision": () => ({ item_id: "ap_1", state: "waiting", decision: { decision: "accept", sent: false, outcome: "refused" } }),
    });
    render(<AssistQueue />);
    await screen.findByText("Dana Buyer");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/Not sent: the send was refused or failed/)).toBeTruthy();
    expect(screen.getByText("Yes, Friday works.")).toBeTruthy();
  });

  it("a reply shows Confirm, Edit and Later; the rarer actions sit under More, the same on every card; a card asks for your line", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [reply, card] }) });
    render(<AssistQueue />);
    expect(await screen.findByText("Dana Buyer")).toBeTruthy();
    expect(screen.getByText("Can you ship Friday?")).toBeTruthy();
    expect(screen.getByText("Yes, Friday works.")).toBeTruthy();
    for (const name of ["Confirm", "Edit"]) expect(screen.getAllByRole("button", { name }).length).toBe(1);
    expect(screen.getAllByRole("button", { name: "Later" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
    await more(0);
    const entries = screen.getAllByRole("menuitem").map((e) => e.textContent);
    expect(entries).toEqual(["Copy", "Open in Outlook", "Save a preference for my drafts", "Make it a task", "Not needed", "Never from this sender"]);
    expect(screen.getByText("Do you agree to 12.50 a case?")).toBeTruthy();
    expect(screen.getByLabelText("What do you want to say?")).toBeTruthy();
    for (const gone of ["Workflows", "Builder", "Schedules", "Review queue", "Everyone"]) expect(screen.queryByRole("tab", { name: gone })).toBeNull();
  });

  it("Confirm accepts the item, Copy puts the reply on the clipboard, and a line on a card goes to reply-from-line", async () => {
    const write = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText: write } });
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [reply, card] }),
      "/api/autopilot/items/ap_1/decision": () => ({ item_id: "ap_1", decision: { decision: "accept", sent: false } }),
      "/api/autopilot/items/ap_2/reply-from-line": () => ({ item_id: "ap_2", decision: { decision: "edit", text: "Yes, 12.50 works." } }),
    });
    render(<AssistQueue />);
    await screen.findByText("Dana Buyer");
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Copy" }));
    await waitFor(() => expect(write).toHaveBeenCalledWith("Yes, Friday works."));
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
    render(<AssistQueue />);
    await screen.findByRole("button", { name: "No, list" });
    // the replies wait behind the answers: nothing is shown or decided before a tap
    expect(screen.queryByText("Sorry, we stay at list.")).toBeNull();
    expect(screen.getByLabelText("Or say it in a few words")).toBeTruthy();
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
    render(<AssistQueue />);
    expect(await screen.findByText("Never mention prices to Kim")).toBeTruthy();
    expect(screen.getByText("Applied")).toBeTruthy();
    expect(screen.getByTestId("draft-learned").textContent).toContain("Starts replies with just the person's name");
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Save a preference for my drafts" }));
    await userEvent.type(screen.getByLabelText(/Save as a preference/), "Always sign off V");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some(([p, b]) => p === "/api/memory" && (b as { kind: string; text: string }).kind === "preference" && (b as { text: string }).text === "Always sign off V")).toBe(true));
    expect(await screen.findByText(/Saved as your preference/)).toBeTruthy();
  });

  it("admins get an Everyone section: who switched what on and each workflow's score, flagged below the bar", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [] }) });
    render(
      <AutopilotView
        area={area({ owner: true, people: [{ id: "p1", name: "Ana", you: false, roles: [] }], drafts_switched_on: [{ person: "p1", choice: "mail_replies", label: "Replies to mail" }, { person: "p1", choice: "teams", label: "Replies in Teams", by_owner: true }], outcomes: { by_autopilot: { customer_question: { accepted: 1, edited: 1, rejected: 10 }, task_assigned: { accepted: 0, edited: 0, rejected: 0 } } } })}
      />,
    );
    await screen.findByRole("heading", { name: "Everyone" });
    expect(screen.getByText("Ana")).toBeTruthy();
    // one line per person, their workflows in it
    expect(screen.getByTestId("ap-who").textContent).toBe("AnaReplies to mail, Replies in Teams (set by the owner)");
    expect(screen.getByText(/1 accepted, 1 edited, 10 rejected/)).toBeTruthy();
    expect(screen.getByText(/keep it off: 2 of 12 kept/)).toBeTruthy();
    expect(screen.queryByText(/0 accepted, 0 edited/)).toBeNull(); // no zero counters
  });

  it("the Everyone section counts what is waiting on each person, from the items the owner already sees", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [] }) });
    const it = (item_id: string, owner: string, state: string) => ({ item_id, owner, state, autopilot: "x", label: "x", title: "x", workflow: "x", system: "mail", roles: [], mine: false, happened: {}, prepared: {}, citations: [], due_at: "", created_at: "", today: true, live_kinds: [], live: false });
    render(
      <AutopilotView
        area={area({ owner: true, people: [{ id: "p1", name: "Ana", you: false, roles: [] }, { id: "p2", name: "Sam", you: false, roles: [] }],
          items: [it("a", "p1", "waiting"), it("b", "p2", "waiting"), it("c", "p2", "edited"), it("d", "p2", "accepted"), it("e", "p1", "snoozed")] })}
      />,
    );
    await screen.findByRole("heading", { name: "Everyone" });
    expect(screen.getByTestId("ap-waiting").textContent).toBe("Sam2 waitingAna1 waiting");
    // nothing accepted, edited or rejected yet: no score section saying so
    expect(screen.queryByRole("heading", { name: "How each one is doing" })).toBeNull();
  });

  it("a choice the owner's Draft file turned on shows on, set by the owner; an empty queue points to the switches", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [] }) });
    const drafts = [
      { choice: "customer_replies", label: "Replies to customers", on: true, by_owner: true },
      { choice: "teams_replies", label: "Replies in Teams", on: true, by_owner: false, since: "2026-11-18T19:00:00Z" },
      { choice: "collections", label: "Collections and invoices", on: false },
      { choice: "approvals", label: "Approvals", on: true, partly: true, by_owner: true },
    ];
    render(<AutopilotView area={area({ my_drafts: drafts, checked_at: "2026-11-18T23:14:34Z" })} />);
    const states = (await screen.findAllByTestId("draft-choice-state")).map((e) => e.textContent);
    expect(states[0]).toBe("On, set by the owner. Prepared for you; waits for your confirm.");
    expect(states[1]).toMatch(/^On since .*\. Prepared for you; waits for your confirm\.$/);
    expect(states[2]).toBe("Off. Nothing is prepared for you.");
    expect(states[3]).toBe("Partly on, set by the owner. Prepared for you; waits for your confirm.");
    expect(screen.getByRole("switch", { name: "Approvals: on" }).textContent).toBe("Turn off");
    expect(screen.getByRole("switch", { name: "Replies to customers: on" }).textContent).toBe("Turn off");
    expect(screen.queryByTestId("prepared-empty")).toBeNull(); // the items are on Home, not here
    cleanup();
    render(<AssistQueue />);
    expect((await screen.findByTestId("prepared-empty")).textContent).toBe("Nothing is waiting for you. What gets prepared is set on Switches.");
  });

  it("an empty queue while the feed behind it is stopped does not read as nothing to do", async () => {
    forgetFeeds();
    fetchMock({
      "/api/autopilot/drafts": () => ({ items: [] }),
      "/api/feeds": () => ({ checked_at: "x", feeds: [{ key: "autopilot", label: "Autopilot entries", state: "not_running", why: "The autopilot's feed has stopped.", since: "2026-11-21T06:26:00Z" }] }),
    });
    render(<AssistQueue />);
    expect((await screen.findByTestId("prepared-empty")).textContent).toMatch(/^Nothing is waiting for you, but nothing new is being prepared either/);
    forgetFeeds();
  });

  it("a switched-on choice picks confirm or auto, auto only once its replay score earned it", async () => {
    const score = { judged: 40, matched: 38, percent: 95, need_judged: 15, need_percent: 90, source: "fresh set A", measured_on: "2026-11-21", window: ["2026-11-06T15:00:00Z", "2026-11-19T20:00:00Z"] };
    const earned = { choice: "supplier_followups", label: "Supplier follow-ups", on: true, mode: "confirm", auto_ok: true, score };
    const all = area({ my_drafts: [earned, { choice: "teams_replies", label: "Replies in Teams", on: true, mode: "confirm", auto_ok: false }, { choice: "collections", label: "Collections and invoices", on: false, mode: "off" }] });
    const calls = fetchMock({ "/api/autopilot/drafts": () => ({ items: [] }), "/api/autopilot/mode": () => all });
    render(<AutopilotView area={all} />);
    const groups = await screen.findAllByTestId("draft-choice-mode");
    expect(groups).toHaveLength(2); // an off choice has no mode to pick
    const autos = screen.getAllByRole("radio", { name: /^Auto/ }) as HTMLInputElement[];
    expect(autos.map((r) => r.disabled)).toEqual([false, true]);
    expect(screen.getByTestId("draft-choice-score").textContent).toMatch(/38 of 40/);
    // a person whose own score passes but the category's does not sees the team score
    expect(optInScore({ eligible: false, score: { ...score, judged: 30, matched: 12, percent: 40, scope: "team" } })).toMatch(/^Not eligible: team score 12\/30 \(40\.0%\)/);
    // the number beside the switch says where and when it was measured
    expect(screen.getByTestId("draft-choice-score").textContent).toMatch(/6 Nov 2026 to 19 Nov 2026 on 21 Nov 2026 \(fresh set A\)/);
    await userEvent.click(autos[0]);
    await waitFor(() => expect(calls.some(([p, b]) => p === "/api/autopilot/mode" && JSON.stringify(b) === JSON.stringify({ choice: "supplier_followups", mode: "auto" }))).toBe(true));
    await userEvent.click(screen.getByRole("switch", { name: "Collections and invoices: off" }));
    await waitFor(() => expect(calls.some(([p, b]) => p === "/api/autopilot/mode" && (b as { mode: string }).mode === "confirm")).toBe(true));
  });

  it("scoreOf flags a workflow only with ten settled and under a fifth kept", () => {
    expect(scoreOf({ accepted: 1, edited: 0, rejected: 9 }).off).toBe(true);
    expect(scoreOf({ accepted: 2, edited: 0, rejected: 8 }).off).toBe(false); // exactly a fifth
    expect(scoreOf({ accepted: 0, edited: 0, rejected: 9 }).off).toBe(false); // too few
  });

  it("Later snoozes the item to tomorrow 8 AM and Not needed rejects it; each leaves the list", async () => {
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [reply, card] }),
      "/api/autopilot/items/ap_1/decision": () => ({ item_id: "ap_1", decision: { decision: "snooze" } }),
      "/api/autopilot/items/ap_2/decision": () => ({ item_id: "ap_2", decision: { decision: "reject" } }),
    });
    render(<AssistQueue />);
    await screen.findByText("Dana Buyer");
    await userEvent.click(screen.getAllByRole("button", { name: "Later" })[0]);
    await waitFor(() => expect(screen.queryByText("Dana Buyer")).toBeNull());
    const [, snooze] = calls.find(([p]) => p === "/api/autopilot/items/ap_1/decision")!;
    expect((snooze as { decision: string }).decision).toBe("snooze");
    expect(new Date((snooze as { until: string }).until).getHours()).toBe(8);
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Not needed" }));
    await waitFor(() => expect(screen.queryByText("Lee Supplier")).toBeNull());
    expect(calls.some(([p, b]) => p === "/api/autopilot/items/ap_2/decision" && (b as { decision: string }).decision === "reject")).toBe(true);
  });

  it("an alert shows what happened, why, where it is decided and its records; it is only set aside", async () => {
    const alert: PreparedItem = { ...reply, item_id: "ap_9", sender: "", channel: "system", kind: "alert", subject: "QC hold: Lot L-1", message: "", body: "", to: [], summary: "Lot L-1 is on-hold; it holds up 1 open order.", facts: ["OR-9, Three Ships"], note: "Release it in the Portal.", links: [{ label: "Open the order", href: "/object/SalesOrder:s1" }] };
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [alert] }),
      "/api/autopilot/items/ap_9/decision": () => ({ item_id: "ap_9", decision: { decision: "reject" } }),
    });
    render(<AssistQueue />);
    expect(await screen.findByText("Lot L-1 is on-hold; it holds up 1 open order.")).toBeTruthy();
    expect(screen.getByText("OR-9, Three Ships")).toBeTruthy();
    expect(screen.getByText("Release it in the Portal.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open the order" }).getAttribute("href")).toBe("/object/SalesOrder:s1");
    expect(screen.queryByRole("button", { name: /Confirm|Send|Done/ })).toBeNull();
    await more(0);
    expect(screen.getAllByRole("menuitem").map((e) => e.textContent)).toEqual(["Pass to a colleague", "Make it a task", "Not needed"]);
    await userEvent.click(screen.getByRole("menuitem", { name: "Not needed" }));
    await waitFor(() => expect(screen.queryByText("QC hold: Lot L-1")).toBeNull());
    expect(calls.filter(([p]) => p.endsWith("/decision")).map(([, b]) => (b as { decision: string }).decision)).toEqual(["reject"]);
  });

  it("a reply shows the dry run of Auto when its workflow was earned", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [{ ...reply, item_id: "ap_d1", auto_would: "keep: term:money" }, { ...reply, item_id: "ap_d2", subject: "Two", auto_would: "send" }] }) });
    render(<AssistQueue />);
    const shown = await screen.findAllByTestId("auto-would");
    expect(screen.getByTestId("auto-digest").textContent).toBe("Dry run of Auto: 1 of these would go by themselves, 1 would still wait for you. Nothing is sent from here without your click.");
    expect(shown.map((n) => n.textContent)).toEqual(["With Auto on, this would still wait for you.", "With Auto on, this would go by itself."]);
  });

  it("a record an alert is about can be followed from the card; nothing is decided", async () => {
    const alert: PreparedItem = { ...reply, item_id: "ap_9", sender: "", channel: "system", kind: "alert", subject: "QC hold: Lot L-1", message: "", body: "", to: [], summary: "Lot L-1 is on-hold.", facts: [], links: [{ label: "Open the order", href: "/object/SalesOrder:s%201" }, { label: "Open the task", href: "/tasks/t1" }] };
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [alert] }),
      "/api/watch/follow": (b) => ({ object_id: (b as { object_id: string }).object_id }),
    });
    render(<AssistQueue />);
    await screen.findByText("Lot L-1 is on-hold.");
    expect(screen.queryByRole("button", { name: "Follow Open the task" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Follow Open the order" }));
    expect(await screen.findByText("Following")).toBeTruthy();
    expect(calls.find(([p]) => p === "/api/watch/follow")?.[1]).toEqual({ object_id: "SalesOrder:s 1" });
    expect(calls.some(([p]) => p.endsWith("/decision"))).toBe(false);
  });

  it("an alert is passed to a colleague by an opaque key; nothing is decided", async () => {
    const alert: PreparedItem = { ...reply, item_id: "ap_9", sender: "", channel: "system", kind: "alert", subject: "QC hold: Lot L-1", message: "", body: "", to: [], summary: "Lot L-1 is on-hold.", facts: [], links: [] };
    const passed: PreparedItem = { ...alert, item_id: "ap_8", subject: "Approval: PO 7", summary: "Approve PO 7.", handed_from: "Ana" };
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [alert, passed] }),
      "/api/autopilot/colleagues": () => ({ people: [{ key: "k-sam", name: "Sam" }] }),
      "/api/autopilot/items/ap_9/hand": () => ({ items: [{ ...alert, handed_to: "Sam" }] }),
    });
    render(<AssistQueue />);
    await screen.findByText("Lot L-1 is on-hold.");
    expect(screen.getByTestId("alert-handed-from")).toHaveTextContent("Passed to you by Ana");
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Pass to a colleague" }));
    await userEvent.click(await screen.findByRole("button", { name: "Pass on" }));
    expect(await screen.findByTestId("alert-handed-to")).toHaveTextContent("Passed to Sam");
    expect(calls.find(([p]) => p === "/api/autopilot/items/ap_9/hand")?.[1]).toEqual({ to: "k-sam" });
    expect(calls.some(([p]) => p.endsWith("/decision"))).toBe(false);
  });

  it("Never from this sender hides what they send until Prepare again; nothing is decided", async () => {
    let never: string[] = [];
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: never.length ? [card] : [reply, card], never_senders: never }),
      "/api/autopilot/never": (b) => {
        const { sender, never: on } = b as { sender: string; never: boolean };
        never = on ? [sender] : [];
        return { items: [], never_senders: never };
      },
    });
    render(<AssistQueue />);
    await screen.findByText("Dana Buyer");
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Never from this sender" }));
    await waitFor(() => expect(screen.queryByText("Dana Buyer")).toBeNull());
    expect(calls.some(([p, b]) => p === "/api/autopilot/never" && (b as { sender: string; never: boolean }).sender === "Dana Buyer" && (b as { never: boolean }).never === true)).toBe(true);
    expect(screen.getByText("Nothing is prepared for you from Dana Buyer.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Prepare again" }));
    await screen.findByText("Dana Buyer");
    expect(calls.some(([p]) => p.endsWith("/decision"))).toBe(false);
  });

  it("a prepared-changes card from the Assistant can be set aside: Not needed rejects it and it leaves the list", async () => {
    const job: PreparedItem = { ...reply, item_id: "ap_3", kind: "job", body: "", summary: "Move PO 7 to 20 November.", jobs: [{ index: 0, tool: "plx.prepare_write", system: "portal", operation: "purchase-order.update", reason: "The supplier confirmed 20 November.", target: { id: "cmpo0001" }, change: { expectedDate: "2026-11-20" } }] };
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [job] }),
      "/api/autopilot/items/ap_3/decision": () => ({ item_id: "ap_3", decision: { decision: "reject" } }),
    });
    render(<AssistQueue />);
    expect(await screen.findByRole("button", { name: "Review this change" })).toBeTruthy();
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Not needed" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Review this change" })).toBeNull());
    expect(calls.some(([p, b]) => p === "/api/autopilot/items/ap_3/decision" && (b as { decision: string }).decision === "reject")).toBe(true);
    expect(calls.some(([p]) => p.includes("/jobs/"))).toBe(false);
  });

  it("an approval with proposed changes is one card: the recommendation, its reasons, the Mission Control link and the change", async () => {
    const job: PreparedItem = { ...reply, item_id: "ap_4", kind: "job", body: "", summary: "Approval requested for TASK-12: ship the release", facts: ["Recommended: approve", "an earlier approval of this task was granted on 2026-11-19"], note: "Decide in Mission Control.", links: [{ label: "Decide in Mission Control", href: "https://mc.plxcustomer.io/tasks/TASK-12" }], jobs: [{ index: 0, tool: "plx.prepare_write", system: "planner", operation: "task_create", reason: "Follow-up after the release.", target: {}, change: { title: "Release follow-up" } }] };
    const calls = fetchMock({ "/api/autopilot/drafts": () => ({ items: [job] }) });
    render(<AssistQueue />);
    expect(await screen.findByText("Recommended: approve")).toBeTruthy();
    expect(screen.getAllByTestId("prepared-job")).toHaveLength(1);
    expect(screen.getByText("an earlier approval of this task was granted on 2026-11-19")).toBeTruthy();
    const link = screen.getByRole("link", { name: "Decide in Mission Control" });
    expect(link.getAttribute("href")).toBe("https://mc.plxcustomer.io/tasks/TASK-12");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(screen.getByRole("button", { name: "Review this change" })).toBeTruthy();
    expect(calls.some(([p]) => p.includes("/jobs/") || p.endsWith("/decision"))).toBe(false);
  });

  it("a reply the model wrote can be rewritten from a line; the new reply is shown to edit and confirm", async () => {
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [{ ...reply, redo: true }] }),
      "/api/autopilot/items/ap_1/reply-from-line": () => ({ item_id: "ap_1", decision: { decision: "edit", text: "Friday is tight; Monday works." } }),
    });
    render(<AssistQueue />);
    await screen.findByText("Dana Buyer");
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Rewrite from a line" }));
    await userEvent.type(screen.getByLabelText("Rewrite it: what do you want to say?"), "say Monday");
    await userEvent.click(screen.getByRole("button", { name: "Rewrite" }));
    await waitFor(() => expect(calls.some(([p, b]) => p.endsWith("/ap_1/reply-from-line") && (b as { line: string }).line === "say Monday")).toBe(true));
    expect(((await screen.findByLabelText("Reply")) as HTMLTextAreaElement).value).toBe("Friday is tight; Monday works.");
  });

  it("a reply without redo has no Rewrite", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: [reply] }) });
    render(<AssistQueue />);
    await screen.findByText("Dana Buyer");
    await more(0);
    expect(screen.queryByRole("menuitem", { name: "Rewrite from a line" })).toBeNull();
  });

  it("a reply with other versions lets you pick one; Confirm sends the one you picked", async () => {
    const options = [{ label: "Decline", reply: "Sorry, Friday is not possible." }];
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [{ ...reply, options }] }),
      "/api/autopilot/items/ap_1/decision": () => ({ item_id: "ap_1", decision: { decision: "accept", sent: false } }),
    });
    render(<AssistQueue />);
    await userEvent.click(await screen.findByRole("button", { name: "Decline" }));
    expect(screen.getByText("Sorry, Friday is not possible.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.some(([p, b]) => p.endsWith("/ap_1/decision") && (b as { text?: string }).text === "Sorry, Friday is not possible.")).toBe(true));
  });

  it("Make it a task keeps it as your own reminder and takes it off the queue, never scored as wrong", async () => {
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [reply] }),
      "/api/memory": () => ({ id: 9, kind: "commitment", text: "x" }),
      "/api/autopilot/items/ap_1/decision": () => ({ item_id: "ap_1", decision: { decision: "reject" } }),
    });
    render(<AssistQueue />);
    await screen.findByText("Dana Buyer");
    await more(0);
    await userEvent.click(screen.getByRole("menuitem", { name: "Make it a task" }));
    await waitFor(() => expect(screen.queryByText("Dana Buyer")).toBeNull());
    expect(calls.filter(([p]) => p !== "/api/autopilot/drafts")).toEqual([
      ["/api/memory", { kind: "commitment", text: "Remind me to follow up: PO 7 (Dana Buyer)" }],
      ["/api/autopilot/items/ap_1/decision", { decision: "reject", note: "made a task" }],
    ]);
  });

  it("Always do this is offered only where Auto was earned, for the sender or the whole switch", async () => {
    const earned = { ...reply, sender: "dana@customer.example", category: "customer_replies", category_label: "Replies to customers", always_ok: true };
    const calls = fetchMock({
      "/api/autopilot/drafts": () => ({ items: [earned, { ...earned, item_id: "ap_5", always_ok: false }] }),
      "/api/autopilot/items/ap_1/always": () => area(),
    });
    render(<AssistQueue />);
    await screen.findAllByText("dana@customer.example");
    await more(1);
    expect(screen.queryByRole("menuitem", { name: /Always/ })).toBeNull();
    await more(1);
    await more(0);
    expect(screen.getByRole("menuitem", { name: "Always do this: Replies to customers" })).toBeTruthy();
    await userEvent.click(screen.getByRole("menuitem", { name: "Always do this for dana@customer.example" }));
    expect(await screen.findByText(/Auto is on for dana@customer.example under Replies to customers/)).toBeTruthy();
    expect(calls.find(([p]) => p === "/api/autopilot/items/ap_1/always")?.[1]).toEqual({ scope: "sender" });
  });

  it("limits on Auto are set on the switch: senders, days, hours and a stricter bar; a bad bar is refused before sending", async () => {
    const score = { judged: 40, matched: 38, percent: 95, need_judged: 15, need_percent: 90 };
    const sw = { choice: "supplier_followups", label: "Supplier follow-ups", on: true, mode: "auto", auto_ok: true, score };
    const calls = fetchMock({ "/api/autopilot/conditions": () => area({ my_drafts: [{ ...sw, conditions: { senders: ["@acme.example"], days: [0, 4], start: "08:00", end: "17:00", min_percent: 95 } }] }) });
    render(<AutopilotView area={area({ my_drafts: [sw] })} />);
    expect(screen.getByTestId("auto-limits").textContent).toContain("no limits of your own");
    await userEvent.click(screen.getByRole("button", { name: "Change limits" }));
    await userEvent.type(screen.getByLabelText(/Only for these senders/), "@acme.example");
    await userEvent.click(screen.getByRole("checkbox", { name: "Mon" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Fri" }));
    await userEvent.type(screen.getByLabelText("Your bar"), "lots");
    await userEvent.click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByText("The bar is a percentage, for example 95.")).toBeTruthy();
    expect(calls.filter(([p]) => p === "/api/autopilot/conditions")).toEqual([]);
    await userEvent.clear(screen.getByLabelText("Your bar"));
    await userEvent.type(screen.getByLabelText("Your bar"), "95");
    await userEvent.click(screen.getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(calls.filter(([p]) => p === "/api/autopilot/conditions")).toEqual([["/api/autopilot/conditions", { choice: "supplier_followups", senders: ["@acme.example"], days: [0, 4], start: null, end: null, min_percent: 95 }]]));
    expect(await screen.findByText(/Auto: only for @acme.example; Mon, Fri; 08:00 to 17:00 ET; only work that matched you 95% or more\./)).toBeTruthy();
  });

  it("tomorrowMorning is 8 AM on the next day", () => {
    const d = tomorrowMorning(new Date(2026, 10, 30, 22, 15));
    expect([d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes()]).toEqual([2026, 11, 1, 8, 0]);
  });

  it("mailto carries cc, subject and body", () => {
    expect(mailtoOf({ ...reply, cc: ["a@x.co"] }, "Hi")).toBe("mailto:dana%40customer.example?cc=a%40x.co&subject=RE%3A%20PO%207&body=Hi");
  });
});

describe("a long list draws its first cards and every other one is one click away", () => {
  const many = (n: number): PreparedItem[] => Array.from({ length: n }, (_, k) => ({ ...reply, item_id: `ap_${k}`, subject: `Subject ${k}` }));
  beforeEach(() => {
    vi.unstubAllGlobals();
    nav.params = "";
  });
  afterEach(() => cleanup());

  it("draws 40 of 130, 100 more on a click and all of them on 'Show all'; the count is the whole list", async () => {
    fetchMock({ "/api/autopilot/drafts": () => ({ items: many(130) }) });
    render(<AssistQueue />);
    await screen.findByText("Subject 0");
    expect(screen.getAllByText(/^Subject \d+$/)).toHaveLength(40);
    expect(screen.getByTestId("more-cards").textContent).toContain("90 more");
    expect(screen.getByRole("heading", { name: /Prepared for you/ }).textContent).toContain("130");
    await userEvent.click(screen.getByRole("button", { name: "Show 100 more" }));
    expect(screen.getAllByText(/^Subject \d+$/)).toHaveLength(130);
    expect(screen.queryByTestId("more-cards")).toBeNull();
  });

  it("'Show all' draws every item; a linked item beyond the first cards is drawn at once", async () => {
    nav.params = "item=ap_120";
    fetchMock({ "/api/autopilot/drafts": () => ({ items: many(130) }) });
    render(<AssistQueue />);
    await screen.findByText("Subject 120");
    expect(screen.getAllByText(/^Subject \d+$/)).toHaveLength(121);
    await userEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(screen.getAllByText(/^Subject \d+$/)).toHaveLength(130);
  });

  it("a list of 40 or fewer has no control and loses nothing (N=1, 10, 40)", async () => {
    for (const n of [1, 10, 40]) {
      cleanup();
      fetchMock({ "/api/autopilot/drafts": () => ({ items: many(n) }) });
      render(<AssistQueue />);
      await screen.findByText("Subject 0");
      expect(screen.getAllByText(/^Subject \d+$/)).toHaveLength(n);
      expect(screen.queryByTestId("more-cards")).toBeNull();
    }
  });
});
