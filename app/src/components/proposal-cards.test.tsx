import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MAILTO_MAX, mailtoHref } from "@/lib/proposals";
import type { Proposal, Source } from "@/lib/types";
import { ProposalCards } from "./proposal-cards";

const src = (system: Source["system"], handle: string, label: string): Source => ({ system, handle, label, open_url: `/source?handle=${encodeURIComponent(handle)}` });
const BC = src("BC", "bc://x/po/1", "BC purchase order 1");
const MAIL = src("Email", "graph-mail://x/m/1", "Email from the vendor");
const subject = { object_id: "PurchaseOrder:x:1", type: "PurchaseOrder", label: "PO 1", open_url: "/x" };

const chase = (over: Partial<Proposal> = {}): Proposal => ({
  id: "vendor_chase:PurchaseOrder:x:1",
  kind: "vendor_chase",
  title: "Chase the vendor for PO 1",
  subject,
  to: "bob@vendor.example",
  to_name: "Bob",
  contact_note: "Address from the last message they sent.",
  email_subject: "PO 1: please confirm the ship date",
  body: "Hello Bob,\n\nOur PO 1 was due on 2099-01-15.\n\nPlease confirm the date these items will ship.",
  figures: [{ label: "Original due date", value: "2099-01-15", source: BC }],
  missing: [],
  sent: false,
  ...over,
});

const update: Proposal = {
  id: "record_update:PurchaseOrder:x:1:bc://x/po/1",
  kind: "record_update",
  title: "Update expected receipt date on PO 1 in BC",
  subject,
  change: { system: "BC", record: "PO 1", field: "Expected receipt date", before: "2099-01-15", after: "2099-01-28", before_source: BC, evidence: MAIL, why: "The vendor confirmed 2099-01-28; BC still says 2099-01-15." },
  figures: [],
  missing: [],
  sent: false,
};

describe("ProposalCards", () => {
  it("opens the edited draft in the user's own mail client and copies it; nothing is sent", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText: write } });
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<ProposalCards proposals={[chase()]} />);
    const text = screen.getByLabelText("Draft text");
    fireEvent.change(text, { target: { value: "Hello Bob,\nEdited ask." } });
    const href = screen.getByTestId("open-in-outlook").getAttribute("href")!;
    expect(href.startsWith("mailto:bob@vendor.example?subject=PO%201%3A")).toBe(true);
    expect(decodeURIComponent(href.split("&body=")[1])).toBe("Hello Bob,\nEdited ask.");
    fireEvent.click(screen.getByRole("button", { name: /Copy/ }));
    await waitFor(() => expect(write).toHaveBeenCalledWith("To: bob@vendor.example\nSubject: PO 1: please confirm the ship date\n\nHello Bob,\nEdited ask."));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("says when no contact is known and leaves the address empty", () => {
    render(<ProposalCards proposals={[chase({ to: null, contact_note: "No vendor contact found in the mail, BC or FileMaker records you can see: add the address before sending.", missing: ["The vendor's contact address"] })]} />);
    expect(screen.getByTestId("contact-note").textContent).toMatch(/No vendor contact found/);
    expect(screen.getByTestId("proposal-missing").textContent).toMatch(/contact address/);
    expect(screen.getByTestId("open-in-outlook").getAttribute("href")!.startsWith("mailto:?subject=")).toBe(true);
  });

  it("shows a proposed update as before and after with its evidence", () => {
    render(<ProposalCards proposals={[update]} />);
    const card = screen.getByTestId("proposed-change");
    expect(within(card).getByText(/15 Jan 2099/)).toBeTruthy();
    expect(within(card).getByText(/28 Jan 2099/)).toBeTruthy();
    expect(card.textContent).toMatch(/Make the change in Business Central. The Workbench doesn't write to it./);
    expect(screen.queryByTestId("open-in-outlook")).toBeNull();
  });

  it("renders nothing without proposals", () => {
    const { container } = render(<ProposalCards proposals={[]} />);
    expect(container.innerHTML).toBe("");
  });
});

describe("mailtoHref", () => {
  it("leaves out anything that is not one plain address (no injected headers)", () => {
    for (const bad of ["a@b.example?cc=x@y.example", "a@b.example,c@d.example", "a@b.example&bcc=e@f.example", "not an address"]) {
      expect(mailtoHref(bad, "s", "b").startsWith("mailto:?subject=")).toBe(true);
    }
  });

  it("keeps a long body under the mail client's limit and says it was shortened", () => {
    const href = mailtoHref("a@b.example", "s", "x ".repeat(5000));
    expect(href.length).toBeLessThanOrEqual(MAILTO_MAX);
    expect(decodeURIComponent(href)).toMatch(/shortened: copy the full text/);
  });
});
