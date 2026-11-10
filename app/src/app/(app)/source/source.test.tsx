import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { SourceView } from "@/lib/types";

const params = { handle: "", field: [] as string[] };
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  useSearchParams: () => ({
    get: (k: string) => (k === "handle" ? params.handle : null),
    getAll: (k: string) => (k === "field" ? params.field : []),
  }),
}));
const getSource = vi.fn();
vi.mock("@/lib/api", () => ({ getSource: (...a: unknown[]) => getSource(...a) }));

import SourcePage from "./page";

const mail: SourceView = {
  handle: "graph-mail://box/m1@c1",
  system: "Email",
  label: "Email message",
  kind: "record",
  title: "RE: PO 5275 is fully completed - please come to pick up",
  text: "Thanks Carrie!\n\n@Keenan Bittner<mailto:keenan@example.com> please see below.[cid:image003.png@01DD3AC9.D4D52980]\n______________________________\n\nRegards,\n[cid:image001.png@01DD3AC9.D4D52980]\nRoss\ne: ross@example.com<mailto:ross@example.com>",
  fields: {
    "row.authentication_results": '["spf=fail"]',
    "row.change_key": "CQAAABYAAAAU",
    "row.content_sha256": "c29e7b0c",
    "row.from": "ross@example.com",
    "row.to": '["keenan@example.com", "carrie@example.com"]',
    "row.cc": '["emily@example.com"]',
    "row.sent_at": "2026-09-02T14:57:00Z",
    "row.subject": "RE: PO 5275 is fully completed - please come to pick up",
    "row.id": "AAkALgAAAAAAHYQ",
  },
  as_of: null,
  text_offset: 0,
  next_offset: null,
  changed_since: false,
  access_label: "Viewing with full-read access",
};

const bcPo: SourceView = {
  handle: "bc://production/c/purchaseOrders/p1@e",
  system: "BC",
  label: "BC production",
  kind: "record",
  title: null,
  text: null,
  fields: {
    "row.buyFromAddressLine1": "6781 COLUMBUS ROAD",
    "row.buyFromCity": "MISSISSAUGA",
    "row.currencyCode": "CAD",
    "row.currencyId": "00000000-0000-0000-0000-000000000000",
    "row.discountAmount": "0",
    "row.fullyReceived": "false",
    "row.id": "67d2dd81-f57e",
    "row.lastModifiedDateTime": "2026-07-14T17:42:35.023Z",
    "row.number": "PO100095",
    "row.orderDate": "2026-03-28",
    "row.status": "Open",
    "row.totalAmountIncludingTax": "827.11",
    "row.vendorName": "New Directions Aromatics",
    "row.vendorNumber": "V08380",
    "row.requestedReceiptDate": "2026-07-14",
    "row.description": "x".repeat(400),
  },
  as_of: null,
  text_offset: 0,
  next_offset: null,
  changed_since: false,
  access_label: null,
};

describe("source page", () => {
  // braces: a hook that returns the mock would have vitest call it as a teardown
  beforeEach(() => {
    getSource.mockReset();
  });
  afterEach(() => {
    params.handle = "";
    params.field = [];
  });

  it("opens an email as a message: header and body, never its metadata", async () => {
    params.handle = mail.handle;
    getSource.mockResolvedValue(mail);
    render(<SourcePage />);
    const header = await screen.findByTestId("email-header");
    expect(header).toHaveTextContent("From");
    expect(header).toHaveTextContent("ross@example.com");
    expect(header).toHaveTextContent("keenan@example.com, carrie@example.com");
    expect(header).toHaveTextContent("emily@example.com");
    expect(header).toHaveTextContent("2 Sep");
    // the subject is the title, never "Email message"
    expect(screen.getByTestId("source-title")).toHaveTextContent("RE: PO 5275 is fully completed");
    const body = screen.getByTestId("source-text");
    expect(body).toHaveTextContent("Thanks Carrie!");
    expect(body).toHaveTextContent("@Keenan Bittner please see below.");
    // an inline image placeholder goes wherever it sits; a separator line is a short rule
    expect(body).not.toHaveTextContent(/cid:|mailto:|_____/);
    // the signature folds under "Show signature"
    expect(body).not.toHaveTextContent("Regards");
    expect(screen.getByRole("button", { name: /Show signature/ })).toBeInTheDocument();
    const page = document.body.textContent ?? "";
    for (const internal of ["row.", "authentication", "spf=fail", "change_key", "CQAAABYAAAAU", "sha256", "c29e7b0c", "AAkALgAAAAAAHYQ", "graph-mail://", "full-read"]) {
      expect(page).not.toContain(internal);
    }
  });

  it("opens a record with the cited field first, then number, status, vendor, total with currency, dates", async () => {
    params.handle = bcPo.handle;
    params.field = ["requested receipt date"];
    getSource.mockResolvedValue(bcPo);
    render(<SourcePage />);
    const grid = await screen.findByTestId("source-fields");
    const labels = within(grid).getAllByRole("term").map((t) => t.textContent);
    expect(labels.slice(0, 6)).toEqual(["Requested receipt date", "Number", "Status", "Supplier name", "Total amount including tax", "Currency code"]);
    expect(grid).toHaveTextContent("14 Jul 2026");
    expect(grid).toHaveTextContent("$827.11 CAD");
    expect(grid).toHaveTextContent("28 Mar 2026");
    // the title is the record's number and party, never "BC production"
    expect(screen.getByTestId("source-title")).toHaveTextContent("PO100095 · New Directions Aromatics");
    const page = document.body.textContent ?? "";
    for (const internal of ["row.", "67d2dd81", "00000000-0000", "2026-07-14T17:42", "bc://"]) {
      expect(page).not.toContain(internal);
    }
    // the rest stays one click away, never dumped first
    expect(screen.getByTestId("source-more-fields")).toHaveTextContent("Buy from city");
    // a long value wraps instead of widening the page
    const long = screen.getByText("x".repeat(400));
    expect(long.className).toContain("break-words");
  });

  it("hides an empty BC date and says one total when incl. and excl. tax are equal", async () => {
    params.handle = bcPo.handle;
    getSource.mockResolvedValue({
      ...bcPo,
      fields: { ...bcPo.fields, "row.requestedReceiptDate": "0001-01-01", "row.totalAmountExcludingTax": "827.11" },
    });
    render(<SourcePage />);
    await screen.findByTestId("source-fields");
    const page = document.body.textContent ?? "";
    expect(page).not.toMatch(/Jan 1, 1|1 Jan 1\b|0001/);
    expect(page).not.toContain("Requested receipt date");
    const labels = screen.getAllByRole("term").map((t) => t.textContent);
    expect(labels).toContain("Total");
    expect(labels).not.toContain("Total amount excluding tax");
  });

  it("always shows the cited field, even one named like a key", async () => {
    params.handle = bcPo.handle;
    params.field = ["lot_id"];
    getSource.mockResolvedValue({ ...bcPo, fields: { ...bcPo.fields, "row.lot_id": "L-26101" } });
    render(<SourcePage />);
    const grid = await screen.findByTestId("source-fields");
    const labels = within(grid).getAllByRole("term").map((t) => t.textContent);
    expect(labels[0]).toBe("Lot ID");
    expect(grid).toHaveTextContent("L-26101");
    expect(document.body.textContent).not.toContain("row.lot_id");
  });

  it("a source that isn't there names the page, offers no retry and no question about it", async () => {
    params.handle = "bc://gone@x";
    params.field = [];
    getSource.mockImplementation(async () => { throw new ApiError(404, "not_found"); });
    render(<SourcePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't find that, or you don't have access to it.");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Source");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.queryByPlaceholderText("Ask about this document")).toBeNull();
  });

  it("a server failure still offers a retry", async () => {
    params.handle = "bc://flaky@x";
    getSource.mockImplementation(async () => { throw new ApiError(500, "internal"); });
    render(<SourcePage />);
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
