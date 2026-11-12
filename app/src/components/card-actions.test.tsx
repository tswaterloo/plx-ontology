import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { ActionPreview } from "@/lib/types";
import { ActionDialog } from "./card-actions";

const api = vi.hoisted(() => ({ previewAction: vi.fn(), confirmAction: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));

const preview = (over: Partial<ActionPreview> = {}): ActionPreview => ({
  card_id: "c1",
  kind: "send_email",
  label: "Send email",
  outbound: true,
  sender: "me@plx.example",
  payload: { to: ["a@vendor-a.example"], cc: [], subject: "PO 9", body: "Hello", in_reply_to: null },
  payload_hash: "a".repeat(64),
  citations: [],
  uncited: [],
  unknown_domains: ["vendor-a.example"],
  problems: [],
  missing: [],
  request: null,
  teams_chats: [],
  ...over,
});
const offer = { kind: "send_email", label: "Send email" } as never;
const card = { id: "c1", subject: { label: "PO 9" } };
const open = () => render(<ActionDialog card={card} offer={offer} onClose={() => undefined} onDone={() => undefined} />);

describe("ActionDialog", () => {
  it("an acknowledgement given for one outside domain does not carry to a different one (WEB-010)", async () => {
    api.previewAction.mockReset();
    api.previewAction.mockResolvedValueOnce(preview());
    open();
    const ack = await screen.findByTestId("action-unknown-ok");
    fireEvent.click(ack);
    expect(screen.getByTestId("action-confirm")).not.toBeDisabled();
    // the person changes the recipient to another outside domain and reviews
    api.previewAction.mockResolvedValueOnce(
      preview({ payload: { to: ["b@vendor-b.example"], cc: [], subject: "PO 9", body: "Hello", in_reply_to: null }, payload_hash: "b".repeat(64), unknown_domains: ["vendor-b.example"] }),
    );
    fireEvent.change(screen.getByTestId("action-to"), { target: { value: "b@vendor-b.example" } });
    fireEvent.click(await screen.findByTestId("action-review"));
    await waitFor(() => expect(screen.getByTestId("action-unknown-ok").closest("label")).toHaveTextContent("vendor-b.example"));
    expect(screen.getByTestId("action-unknown-ok")).not.toBeChecked();
    expect(screen.getByTestId("action-confirm")).toBeDisabled();
  });

  it("the same recipients, reviewed again, stay acknowledged", async () => {
    api.previewAction.mockReset();
    api.previewAction.mockResolvedValue(preview());
    open();
    fireEvent.click(await screen.findByTestId("action-unknown-ok"));
    // edit and put the same address back, then review: the recipients are what they were
    fireEvent.change(screen.getByTestId("action-to"), { target: { value: "b@vendor-a.example" } });
    fireEvent.change(screen.getByTestId("action-to"), { target: { value: " A@vendor-a.example " } });
    fireEvent.click(await screen.findByTestId("action-review"));
    await waitFor(() => expect(api.previewAction).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("action-unknown-ok")).toBeChecked();
  });

  it("another address at the same outside domain is not covered by the earlier tick (WEB-010 minor)", async () => {
    api.previewAction.mockReset();
    api.previewAction.mockResolvedValueOnce(preview());
    open();
    fireEvent.click(await screen.findByTestId("action-unknown-ok"));
    api.previewAction.mockResolvedValueOnce(
      preview({ payload: { to: ["eve@vendor-a.example"], cc: [], subject: "PO 9", body: "Hello", in_reply_to: null }, payload_hash: "c".repeat(64) }),
    );
    fireEvent.change(screen.getByTestId("action-to"), { target: { value: "eve@vendor-a.example" } });
    fireEvent.click(await screen.findByTestId("action-review"));
    await waitFor(() => expect(api.previewAction).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("action-unknown-ok")).not.toBeChecked());
    expect(screen.getByTestId("action-confirm")).toBeDisabled();
  });


  it("a failed confirm stays visible after the preview that follows it (WEB-011)", async () => {
    api.previewAction.mockReset();
    api.previewAction.mockResolvedValue(preview({ unknown_domains: [] }));
    api.confirmAction.mockRejectedValue(new ApiError(503, "upstream_unavailable"));
    open();
    fireEvent.click(await screen.findByTestId("action-confirm"));
    const err = await screen.findByTestId("action-confirm-error");
    await waitFor(() => expect(api.previewAction).toHaveBeenCalledTimes(2));
    expect(err).toHaveTextContent("business systems isn't answering");
    expect(screen.getByRole("alert")).toBe(err);
    // a new attempt clears it
    api.confirmAction.mockReturnValue(new Promise(() => undefined));
    fireEvent.click(screen.getByTestId("action-confirm"));
    await waitFor(() => expect(screen.queryByTestId("action-confirm-error")).toBeNull());
  });
});
