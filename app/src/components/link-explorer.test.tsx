import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LinkExplorer } from "./link-explorer";

vi.mock("next/navigation", () => ({ useSearchParams: () => null, useRouter: () => ({ push: vi.fn() }) }));
afterEach(() => vi.unstubAllGlobals());

const node = (id: string, label: string, tier: "firm" | "suggested") => ({ object_id: id, type: "SalesOrder", label, open_url: `/object/SalesOrder/${id}`, tier });
const page = (over: object) => ({ object_id: "x", type: "Customer", label: "Acme", groups: [], partial: false, capped: false, message: null, ...over });

function serve(routes: Record<string, () => Response>) {
  const f = vi.fn(async (url: string) => {
    const hit = Object.entries(routes).find(([k]) => String(url).includes(k));
    return hit ? hit[1]() : new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", f);
  return f;
}
const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });

describe("link explorer", () => {
  it("reads nothing until opened, then groups by kind with firm and suggested styled apart", async () => {
    const f = serve({
      "/api/explore/Customer/c1": () => json(page({ groups: [{ key: "k", label: "Sales orders: ordered by", more: true, nodes: [node("a", "SO 1", "firm"), node("b", "SO 2", "suggested")] }] })),
    });
    render(<LinkExplorer type="Customer" id="c1" label="Acme" />);
    expect(f).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("explorer-open"));
    await screen.findByText("Sales orders: ordered by");
    expect(screen.getByRole("button", { name: /SO 1.*firm link/ }).getAttribute("data-tier")).toBe("firm");
    const sug = screen.getByRole("button", { name: /SO 2.*suggested link/ });
    expect(sug.getAttribute("data-tier")).toBe("suggested");
    expect(sug.className).toContain("border-dashed");
    expect(screen.getByText(/More of this kind exist/)).toBeTruthy();
  });

  it("walks to a neighbour lazily, shows the path and can step back", async () => {
    const f = serve({
      "/api/explore/Customer/c1": () => json(page({ groups: [{ key: "k", label: "Sales orders", nodes: [node("so1", "SO 1", "firm")] }] })),
      "/api/explore/SalesOrder/so1": () => json(page({ object_id: "so1", type: "SalesOrder", label: "SO 1", groups: [{ key: "j", label: "Invoices", nodes: [{ ...node("inv", "INV 9", "firm"), type: "SalesInvoice" }] }] })),
    });
    render(<LinkExplorer type="Customer" id="c1" label="Acme" />);
    fireEvent.click(screen.getByTestId("explorer-open"));
    fireEvent.click(await screen.findByRole("button", { name: /SO 1/ }));
    await screen.findByText("Invoices");
    expect(f).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("explorer-trail").textContent).toContain("Acme");
    fireEvent.click(screen.getByRole("button", { name: "Acme" }));
    await screen.findByText("Sales orders");
  });

  it("says in words, with no number, that a trimmed walk hid something, and an empty one is not 'nothing exists'", async () => {
    serve({ "/api/explore/Customer/c1": () => json(page({ partial: true, message: "Some linked records are not visible to you." })) });
    render(<LinkExplorer type="Customer" id="c1" label="Acme" />);
    fireEvent.click(screen.getByTestId("explorer-open"));
    const note = await screen.findByTestId("explorer-note");
    expect(note.textContent).toBe("Some linked records are not visible to you.");
    expect(note.textContent).not.toMatch(/\d/);
  });

  it("a neighbour the reader cannot open reads as not visible, never a trace", async () => {
    serve({
      "/api/explore/Customer/c1": () => json(page({ groups: [{ key: "k", label: "Orders", nodes: [node("so1", "SO 1", "firm")] }] })),
      "/api/explore/SalesOrder/so1": () => json({ error: { code: "not_found" } }, 404),
    });
    render(<LinkExplorer type="Customer" id="c1" label="Acme" />);
    fireEvent.click(screen.getByTestId("explorer-open"));
    fireEvent.click(await screen.findByRole("button", { name: /SO 1/ }));
    await waitFor(() => expect(screen.getByTestId("explorer-error").textContent).toMatch(/can't see this record/));
  });
});
