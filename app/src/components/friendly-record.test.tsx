import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Suggestion } from "@/lib/types";
import { arrange } from "./ask-box";
import { FriendlyRecord, friendlyRef } from "./friendly-record";
import { ObjectView } from "./object-view";

let params: { ref: string } = { ref: "" };
vi.mock("next/navigation", () => ({
  useParams: () => params,
  useSearchParams: () => null,
  useRouter: () => ({ push: vi.fn() }),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

afterEach(() => vi.unstubAllGlobals());

const sug = (object_id: string, type: string, label: string, sublabel: string): Suggestion => ({
  object_id, type, label, sublabel, match: "exact", score: 1, open_url: `/object/${type}/${encodeURIComponent(object_id)}`,
});
// as the server sends them: two customers of one name (each its own code), a copy of
// the first from the projection without its code, and a location of that name
const DIVERSIFIED = [
  sug('Customer:portal.app_customer:{"id":"fm-cust-89"}', "Customer", "Diversified", "Customer · DIVE11 · BC and Portal"),
  sug('Customer:bc.customers:{"id":"d2"}', "Customer", "Diversified", "Customer · C01100 · BC"),
  { ...sug('Customer:portal.fm_customer:{"id":89}', "Customer", "Diversified", "Customer · Portal"), same_as: ['Customer:portal.app_customer:{"id":"fm-cust-89"}'] },
  sug('CustomerLocation:portal.l:{"id":"x"}', "CustomerLocation", "Diversified", "Customer location · Portal"),
];

const PAGE = {
  object_id: 'Customer:portal.app_customer:{"id":"fm-cust-89"}', type: "Customer", label: "Diversified", summary: "",
  status: { state: "read", text: "Read", sources: [] }, freshness: {}, complete: true,
};

function serve(routes: (url: string) => Response) {
  const f = vi.fn(async (url: string) => routes(String(url)));
  vi.stubGlobal("fetch", f);
  return f;
}

describe("friendly URLs", () => {
  it("takes a business reference, never a whole object id or a path", () => {
    expect(friendlyRef("AO-26253A")).toBe("AO-26253A");
    expect(friendlyRef("Three%20Ships")).toBe("Three Ships");
    for (const bad of ["", "PurchaseOrder:bc.x:{}", "a%2Fb", "%E0%A4%A", "x".repeat(101), "a%0Ab", "a\\b"]) expect(friendlyRef(bad)).toBeNull();
  });

  it("/po/106093 renders the purchase order the API resolves; a bad reference makes no request", async () => {
    const f = serve(() => new Response(JSON.stringify({ ...PAGE, object_id: "PurchaseOrder:1", type: "PurchaseOrder", label: "PO 106093" }), { status: 200 }));
    params = { ref: "106093" };
    render(<FriendlyRecord kind="po" />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("106093");
    expect(f.mock.calls.map((c) => c[0])).toContain("/api/object/PurchaseOrder/106093?quick=1");
    f.mockClear();
    params = { ref: "PurchaseOrder%3Abc.purchase_orders%3A%7B%7D" };
    expect(() => render(<FriendlyRecord kind="po" />)).toThrow("NEXT_NOT_FOUND");
    expect(f).not.toHaveBeenCalled();
  });

  it("a name two customers share opens a chooser naming both, never a guess", async () => {
    serve((url) =>
      url.startsWith("/api/object/")
        ? new Response(JSON.stringify({ error: { code: "not_found" } }), { status: 404 })
        : new Response(JSON.stringify({ query: "Diversified", source: "fixture", took_ms: 1, suggestions: DIVERSIFIED }), { status: 200 }),
    );
    params = { ref: "Diversified" };
    render(<FriendlyRecord kind="customer" />);
    const chooser = await screen.findByTestId("friendly-chooser");
    expect(await screen.findByText("Customer Diversified: 2 records")).toBeInTheDocument();
    const links = chooser.querySelectorAll("a");
    expect([...links].map((a) => a.getAttribute("href"))).toEqual([DIVERSIFIED[0].open_url, DIVERSIFIED[1].open_url]);
    expect(chooser).toHaveTextContent("C01100");
  });

  it("a reference the reader's search does not show says so, naming nothing else", async () => {
    serve((url) =>
      url.startsWith("/api/object/")
        ? new Response(JSON.stringify({ error: { code: "not_found" } }), { status: 404 })
        : new Response(JSON.stringify({ query: "V06110", source: "fixture", took_ms: 1, suggestions: [] }), { status: 200 }),
    );
    params = { ref: "V06110" };
    render(<FriendlyRecord kind="supplier" />);
    expect(await screen.findByText("No supplier “V06110” that you can open")).toBeInTheDocument();
    // one page heading, and a way on from it
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Search for it" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open My work" })).toHaveAttribute("href", "/");
  });
});

describe("a record that is not there", () => {
  it("offers search and the way home, never a retry that cannot help", async () => {
    serve(() => new Response(JSON.stringify({ error: { code: "not_found" } }), { status: 404 }));
    render(<ObjectView type="PurchaseOrder" id="PurchaseOrder:x:00000" />);
    const missing = await screen.findByTestId("record-missing");
    expect(missing).toHaveTextContent("This purchase order isn't here, or you don't have access to it.");
    expect(screen.getByRole("button", { name: "Search for it" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open My work" })).toHaveAttribute("href", "/");
    expect(screen.queryByText("Try again")).toBeNull();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Purchase order");
  });
});

describe("customers who share a name", () => {
  it("the picker keeps two customers of one name apart and says how many share it", () => {
    const rows = arrange(DIVERSIFIED, "diversified");
    expect(rows.map((r) => [r.object_id, r.shared ?? 0])).toEqual([
      [DIVERSIFIED[0].object_id, 2],
      [DIVERSIFIED[1].object_id, 0],
      [DIVERSIFIED[3].object_id, 0],
    ]);
  });

  it("two code-less rows of one name stay apart unless the server says they are one record", () => {
    const a = sug("Customer:bc.customers:{\"id\":\"a\"}", "Customer", "Acme", "Customer");
    const b = sug("Customer:portal.fm_customer:{\"id\":2}", "Customer", "Acme", "Customer · Portal");
    expect(arrange([a, b], "acme")).toHaveLength(2);
    const joined = arrange([a, { ...b, same_as: [a.object_id] }], "acme");
    expect(joined).toHaveLength(1);
    expect(joined[0].systems).toEqual(["Portal"]);
    expect(arrange([a, { ...a }], "acme")).toHaveLength(1);
  });

  it("the record page says so and links to the other one", async () => {
    const other = DIVERSIFIED[1];
    serve(() =>
      new Response(
        JSON.stringify({
          ...PAGE,
          customer: { same_name: [{ object_id: other.object_id, type: "Customer", label: "Diversified (Customer · C01100 · BC)", open_url: other.open_url }] },
        }),
        { status: 200 },
      ),
    );
    render(<ObjectView type="Customer" id="DIVE11" />);
    const line = await screen.findByTestId("same-name");
    expect(line).toHaveTextContent("2 customers share this name. Showing the Customer Portal record.");
    expect(line.querySelector("a")?.getAttribute("href")).toBe(other.open_url);
  });
});
