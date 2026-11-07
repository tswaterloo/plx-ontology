import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/po/90412", params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useSearchParams: () => nav.params,
  useRouter: () => ({ push: () => undefined, replace: () => undefined }),
}));
const auth = vi.hoisted(() => ({ state: { status: "signed-in", me: { feedback_admin: true } } as Record<string, unknown> }));
vi.mock("./auth", () => ({ useAuth: () => ({ state: auth.state }), RequireAuth: ({ children }: { children: unknown }) => children }));

import FeedbackPage from "@/app/(app)/feedback/page";
import type { BriefingItem, FeedbackEntry } from "@/lib/types";
import { FeedbackProvider, isFeedbackShortcut, recordOfPage } from "./feedback-dialog";
import { ItemFeedback, itemBody } from "./item-feedback";

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const posted = (f: ReturnType<typeof vi.fn>, i = 0) => {
  const [url, init] = f.mock.calls[i] as unknown as [string, RequestInit];
  return { url, body: init?.body ? JSON.parse(init.body as string) : undefined };
};

const item = {
  rank: 1,
  id: "gap-7",
  event_type: "late_po",
  label: "Late purchase order",
  object_id: "PurchaseOrder:plx:90412",
  object_type: "PurchaseOrder",
  subject_label: "PO 90412",
  summary: "12 days late",
} as unknown as BriefingItem;

afterEach(() => {
  vi.unstubAllGlobals();
  nav.path = "/po/90412";
});

describe("item feedback", () => {
  it("not useful is one click and takes the item out of the briefing", async () => {
    const f = vi.fn(async () => ok({ ok: true, feedback_id: "fb-1", stored_in: ["feedback_store"] }));
    vi.stubGlobal("fetch", f);
    const marked = vi.fn();
    render(<ItemFeedback item={item} onMarked={marked} />);
    await userEvent.click(screen.getByRole("button", { name: "Not useful" }));
    await waitFor(() => expect(marked).toHaveBeenCalledWith("not_useful"));
    const { url, body } = posted(f);
    expect(url).toBe("/api/feedback/item");
    expect(body).toMatchObject({ item_id: "gap-7", verdict: "not_useful", item_kind: "briefing", object_id: "PurchaseOrder:plx:90412", label: "PO 90412" });
  });

  it("wrong asks what, optionally, before it is marked", async () => {
    const f = vi.fn(async () => ok({ ok: true, feedback_id: "fb-2", stored_in: ["feedback_store"] }));
    vi.stubGlobal("fetch", f);
    const marked = vi.fn();
    render(<ItemFeedback item={item} onMarked={marked} />);
    await userEvent.click(screen.getByRole("button", { name: "Wrong" }));
    expect(f).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText("What is wrong with this item?"), "it was received");
    await userEvent.click(screen.getByRole("button", { name: "Mark wrong" }));
    await waitFor(() => expect(marked).toHaveBeenCalledWith("wrong"));
    expect(posted(f).body).toMatchObject({ verdict: "wrong", note: "it was received" });
    expect(itemBody({ ...item, event_type: "watcher_attention" } as BriefingItem, "useful").item_kind).toBe("watcher");
  });
});

describe("the global Feedback entry", () => {
  it("reads the record from the page path, never the host", () => {
    expect(recordOfPage("/po/90412")).toMatchObject({ object_type: "PurchaseOrder", object_id: "90412" });
    expect(recordOfPage("/object/Customer/C%2F01")).toMatchObject({ object_type: "Customer", object_id: "C/01" });
    expect(recordOfPage("/late")).toBeNull();
  });

  it("F (or Ctrl/⌘+Shift+F) opens it, never while typing", () => {
    const key = (init: KeyboardEventInit, target?: HTMLElement) => {
      const e = new KeyboardEvent("keydown", init);
      if (target) Object.defineProperty(e, "target", { value: target });
      return e;
    };
    expect(isFeedbackShortcut(key({ key: "f" }))).toBe(true);
    expect(isFeedbackShortcut(key({ key: "F", ctrlKey: true, shiftKey: true }))).toBe(true);
    expect(isFeedbackShortcut(key({ key: "f" }, document.createElement("input")))).toBe(false);
    expect(isFeedbackShortcut(key({ key: "f", ctrlKey: true }))).toBe(false);
  });

  it("records the page, the selected record and the note", async () => {
    const f = vi.fn(async () => ok({ ok: true, feedback_id: "fb-3", stored_in: ["feedback_store"] }));
    vi.stubGlobal("fetch", f);
    render(
      <FeedbackProvider>
        <p>page</p>
      </FeedbackProvider>,
    );
    fireEvent.keyDown(window, { key: "f" });
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByTestId("feedback-attached")).toHaveTextContent("Purchase Order 90412");
    await userEvent.type(within(sheet).getByLabelText("What should we know?"), "supplier name is old");
    await userEvent.click(within(sheet).getByTestId("feedback-submit"));
    expect(await within(sheet).findByTestId("feedback-sent")).toBeInTheDocument();
    const { url, body } = posted(f);
    expect(url).toBe("/api/feedback/note");
    expect(body).toMatchObject({ kind: "record", note: "supplier name is old", page: "/po/90412", object_type: "PurchaseOrder", object_id: "90412" });
  });
});

describe("the triage view", () => {
  const entry = (over: Partial<FeedbackEntry>): FeedbackEntry =>
    ({ id: "fb-1", at: "2026-11-06T15:00:00Z", kind: "answer", status: "new", mine: false, reader_name: "Ana Example", rating: -1, question: "is PO 90412 late", note: "wrong date", expected: "12 Nov", claims: [{ i: 0, text: "Due 1 Nov", citations: [] }], flagged: [0], ...over }) as FeedbackEntry;

  it("lists every reader's feedback for the owner and sets a status", async () => {
    nav.path = "/feedback";
    const f = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return ok(entry({ status: "accepted" }));
      return ok({ all_readers: true, total: 2, offset: 0, counts: { new: 2, accepted: 0, fixed: 0, wont_fix: 0 }, entries: [entry({}), entry({ id: "fb-2", kind: "alert", verdict: "wrong", label: "PO 90412", rating: null })] });
    });
    vi.stubGlobal("fetch", f);
    render(
      <FeedbackProvider>
        <FeedbackPage />
      </FeedbackProvider>,
    );
    const rows = await screen.findAllByTestId("feedback-row");
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveTextContent("Wrong: PO 90412");
    const detail = screen.getAllByTestId("feedback-detail")[0];
    expect(detail).toHaveTextContent("12 Nov");
    expect(detail).toHaveTextContent("Marked wrong:");
    await userEvent.click(within(within(detail).getByTestId("feedback-status")).getByRole("radio", { name: "Accepted" }));
    await waitFor(() => expect(f.mock.calls.some(([u, i]) => u === "/api/feedback/fb-1/status" && (i as RequestInit)?.method === "POST")).toBe(true));
    await userEvent.selectOptions(screen.getByLabelText("Type"), "alert");
    await waitFor(() => expect(f.mock.calls.some(([u]) => String(u).includes("kind=alert"))).toBe(true));
  });
});
