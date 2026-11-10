import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { RecordPack } from "@/lib/types";

const pack = vi.hoisted(() => ({ value: null as unknown }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getRecordPack: () => Promise.resolve(pack.value),
}));

import { DocumentsSection } from "./documents";

describe("a record's documents", () => {
  it("names each document and where it was found, opens it, and never shows its citation handle", async () => {
    pack.value = {
      object_id: "PurchaseOrder:1", object_type: "PurchaseOrder", label: "PO 1", basis: "Found around PO 1 as you may read it.", records_read: 3, sent: false, writes: 0,
      documents: [
        { citation: "graph-mail://box/m1@att2", family: "invoice", kind: "attachment", label: "Invoice", title: "INV-5521.pdf", date: "2026-10-21", via: "PurchaseOrder:1" },
        { citation: "sharepoint://site/coa-1", family: "coa", kind: "sharepoint_file", label: "Certificate of analysis", title: "Certificate of analysis", url: "https://contoso.sharepoint.com/x.pdf", via: "PurchaseOrder:1" },
      ],
      missing_labels: ["Packing slip"],
      draft: { body: "Hello,\\nAttached.", channel: "email", status: "draft_not_sent", subject: "PO 1 documents", to: null },
    } satisfies RecordPack;
    render(<DocumentsSection type="PurchaseOrder" id="PurchaseOrder:1" />);
    await userEvent.click(screen.getByRole("button", { name: "Gather the documents" }));
    const [inv, coa] = await screen.findAllByTestId("pack-document");
    expect(inv).toHaveTextContent("Invoice: INV-5521.pdfEmail attachment · 21 Oct");
    expect(screen.getByRole("link", { name: "Invoice: INV-5521.pdf" })).toHaveAttribute("href", "/source?handle=graph-mail%3A%2F%2Fbox%2Fm1%40att2");
    // a title that only repeats the label is said once
    expect(coa).toHaveTextContent(/^Certificate of analysisSharePoint file$/);
    expect(screen.getByTestId("record-pack").textContent).not.toMatch(/graph-mail:|sharepoint:\/\/|sharepoint_file/);
    expect(screen.getByRole("heading", { level: 3 })).toHaveTextContent("Drafted reply · not sent");
    expect(screen.getByTestId("record-pack")).toHaveTextContent("No address on record that you can see");
  });
});
