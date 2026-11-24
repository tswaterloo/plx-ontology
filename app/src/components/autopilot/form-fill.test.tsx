import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ fill: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  fillForm: api.fill,
}));

import { FormFill } from "./form-fill";

const pick = (file: File) => fireEvent.change(screen.getByTestId("form-fill-file"), { target: { files: [file] } });

describe("Fill a form", () => {
  beforeEach(() => api.fill.mockReset());

  it("picks the form with a labelled button, names the chosen file and fills only with a record", async () => {
    api.fill.mockResolvedValue({
      original: "order.docx", name: "order filled.docx", kind: "docx", size: 10, record: { label: "PO 1234", open_url: "/po/1234" },
      counts: { filled: 0 }, fields: [], notes: [],
    });
    render(<FormFill />);
    // no bare browser file control on the page: a button opens the hidden picker
    expect(screen.getByTestId("form-fill-file")).not.toBeVisible();
    expect(screen.getByRole("button", { name: "Choose the form" })).toBeInTheDocument();
    const fill = screen.getByRole("button", { name: "Fill" });
    expect(fill).toBeDisabled();
    pick(new File(["x"], "order.docx"));
    expect(screen.getByTestId("form-fill-name")).toHaveTextContent("order.docx");
    expect(screen.getByRole("button", { name: "Form: order.docx. Choose another" })).toBeInTheDocument();
    expect(fill).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Record"), { target: { value: " PO 1234 " } });
    fireEvent.click(fill);
    await waitFor(() => expect(api.fill).toHaveBeenCalledTimes(1));
    expect(api.fill.mock.calls[0][0]).toMatchObject({ name: "order.docx", record: "PO 1234" });
    expect(await screen.findByTestId("fill-review")).toBeInTheDocument();
  });

  it("refuses a form over the size limit before sending it, and says why", () => {
    render(<FormFill />);
    const big = new File(["x"], "huge.pdf");
    Object.defineProperty(big, "size", { value: 26 * 1024 * 1024 });
    pick(big);
    fireEvent.change(screen.getByLabelText("Record"), { target: { value: "PO 1" } });
    fireEvent.click(screen.getByRole("button", { name: "Fill" }));
    expect(screen.getByRole("alert")).toHaveTextContent("huge.pdf is larger than 25 MB.");
    expect(api.fill).not.toHaveBeenCalled();
  });

  it("offers the reply that carries the filled copy; no send button when it cannot carry it", async () => {
    const base = {
      original: "order.docx", name: "order (filled).docx", kind: "docx", size: 10, record: { label: "PO 1234", open_url: "/po/1234" },
      counts: { filled: 1 }, fields: [], notes: [], download_url: "/api/instruct/files/f_1",
    };
    api.fill.mockResolvedValue({
      ...base,
      reply: { to: ["pat@maple.example"], subject: "RE: PO 1234", attachment: "order (filled).docx", status: "ready", missing: [], card_id: "fill:up_1",
        actions: [{ kind: "reply_in_thread", label: "Reply in thread", outbound: true }] },
    });
    render(<FormFill />);
    pick(new File(["x"], "order.docx"));
    fireEvent.change(screen.getByLabelText("Record"), { target: { value: "PO 1234" } });
    fireEvent.click(screen.getByRole("button", { name: "Fill" }));
    const reply = await screen.findByTestId("fill-reply");
    expect(reply).toHaveTextContent("Reply to pat@maple.example with order (filled).docx attached. You see the message before it goes.");
    expect(screen.getByTestId("action-reply_in_thread")).toHaveTextContent("Reply in thread");
  });
});
