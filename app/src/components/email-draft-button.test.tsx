import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const prepareEmailDraft = vi.fn();
vi.mock("@/lib/api", () => ({
  prepareEmailDraft: (...a: unknown[]) => prepareEmailDraft(...a),
}));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => false }));
vi.mock("@/components/card-actions", () => ({
  ActionDialog: ({ card, offer, onClose }: { card: { id: string; subject: { label: string } }; offer: { kind: string }; onClose: () => void }) => (
    <div data-testid="mock-action-dialog">
      <span>{card.id}</span>
      <span>{offer.kind}</span>
      <button type="button" onClick={onClose}>Close</button>
    </div>
  ),
}));

import { EmailDraftButton } from "./email-draft-button";

describe("EmailDraftButton", () => {
  it("drafts an email with the chart and opens confirm-before-send ActionDialog for send_email only", async () => {
    prepareEmailDraft.mockResolvedValue({
      card_id: "brief:abc123",
      kind: "send_email",
      subject: "Open PO spend",
      body: "Summary",
      to: "vince@plx.example",
      attachment_name: "Open PO spend.xlsx",
      upload_id: "up_1",
    });
    render(<EmailDraftButton title="Open PO spend" summary="Acme leads." charts={[{ kind: "bar" }]} />);
    fireEvent.click(screen.getByTestId("email-draft-btn"));
    const dlg = await screen.findByTestId("mock-action-dialog");
    expect(dlg).toHaveTextContent("brief:abc123");
    expect(dlg).toHaveTextContent("send_email");
    await waitFor(() => expect(prepareEmailDraft).toHaveBeenCalledWith(expect.objectContaining({
      title: "Open PO spend",
      summary: "Acme leads.",
      format: "xlsx",
    })));
  });
});
