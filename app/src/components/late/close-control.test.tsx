import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ get: vi.fn(), close: vi.fn(), reopen: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getPoClosure: api.get, closePo: api.close, reopenPo: api.reopen }));

import { CloseControl } from "./close-control";

describe("closing a purchase order", () => {
  beforeEach(() => vi.resetAllMocks());

  it("closes with an optional reason, says who and when, and says nothing is written to the source systems", async () => {
    api.get.mockResolvedValue({ closed: false, new_since: [] });
    api.close.mockResolvedValue({ closed: true, by: "Sam", at: "2026-11-24T15:00:00Z", reason: "received", new_since: [] });
    const changed = vi.fn();
    render(<CloseControl order={{ object_id: "po1" }} onChanged={changed} />);
    fireEvent.click(await screen.findByTestId("po-close-open"));
    expect(document.body.textContent).toMatch(/Nothing changes in Business Central/);
    fireEvent.change(screen.getByPlaceholderText("Why (optional)"), { target: { value: "received" } });
    fireEvent.click(screen.getByTestId("po-close-confirm"));
    await waitFor(() => expect(screen.getByTestId("po-closed").textContent).toMatch(/Closed by Sam on .* \(received\)/));
    expect(api.close).toHaveBeenCalledWith({ object_id: "po1" }, "received");
    expect(changed).toHaveBeenCalled();
  });

  it("shows a closed order's new activity without reopening it, and can undo the close", async () => {
    api.get.mockResolvedValue({ closed: true, by: "Sam", at: "2026-11-24T15:00:00Z", reason: null, new_since: ["A receipt was recorded after it was closed"] });
    api.reopen.mockResolvedValue({ closed: false, new_since: [] });
    render(<CloseControl order={{ object_id: "po1" }} />);
    expect((await screen.findByTestId("po-closed-new")).textContent).toMatch(/A receipt was recorded after it was closed\. It stays closed until you open it again/);
    fireEvent.click(screen.getByText("Open it again"));
    await waitFor(() => expect(screen.getByTestId("po-close-open")).toBeTruthy());
  });

  it("says why a close failed and keeps the order as it was", async () => {
    api.get.mockResolvedValue({ closed: false, new_since: [] });
    api.close.mockRejectedValue({ code: "not_found" });
    render(<CloseControl order={{ handle: "h" }} />);
    fireEvent.click(await screen.findByTestId("po-close-open"));
    fireEvent.click(screen.getByTestId("po-close-confirm"));
    await waitFor(() => expect(document.body.textContent).toMatch(/Couldn't find that, or you don't have access to it/));
    expect(screen.queryByTestId("po-closed")).toBeNull();
  });

  it("supports popover mode with quick-pick reasons, Enter to submit, and Escape to dismiss", async () => {
    api.get.mockResolvedValue({ closed: false, new_since: [] });
    api.close.mockResolvedValue({ closed: true, by: "Sam", at: "2026-11-24T15:00:00Z", reason: "Received", new_since: [] });
    render(<CloseControl order={{ object_id: "po2" }} popover />);
    fireEvent.click(await screen.findByTestId("po-close-open"));
    expect(screen.getByTestId("po-close-popover")).toBeInTheDocument();
    // Escape dismisses
    fireEvent.keyDown(screen.getByPlaceholderText("Why (optional)"), { key: "Escape" });
    expect(screen.queryByTestId("po-close-popover")).toBeNull();
    // Reopen, pick quick reason chip, press Enter
    fireEvent.click(screen.getByTestId("po-close-open"));
    fireEvent.click(screen.getByRole("button", { name: "Received" }));
    fireEvent.keyDown(screen.getByPlaceholderText("Why (optional)"), { key: "Enter" });
    await waitFor(() => expect(api.close).toHaveBeenCalledWith({ object_id: "po2" }, "Received"));
  });
});
