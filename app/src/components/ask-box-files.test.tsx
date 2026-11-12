import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Attachment } from "@/lib/attachments";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

// a file read the test finishes by hand
const reads: { names: string[]; done: (a: Attachment[]) => void }[] = [];
vi.mock("@/lib/attachments", async (orig) => ({
  ...(await orig<typeof import("@/lib/attachments")>()),
  addFiles: (files: Iterable<File>, have: Attachment[]) =>
    new Promise((resolve) => {
      const names = [...files].map((f) => f.name);
      reads.push({ names, done: (added) => resolve({ next: [...have, ...added], problems: [] }) });
    }),
}));

import { AskBox } from "./ask-box";

const att = (name: string): Attachment => ({ id: name, name, size: 1, image: false, data: "QQ==" });
const pdf = (name: string) => new File(["x"], name, { type: "application/pdf" });
const pick = (...names: string[]) => fireEvent.change(screen.getByTestId("attach-input"), { target: { files: names.map(pdf) } });
const finish = async (n: number, ...names: string[]) => {
  await waitFor(() => expect(reads.length).toBeGreaterThan(n));
  await act(async () => reads[n].done(names.map(att)));
};

describe("attachments in the box (WEB-023)", () => {
  it("waits for a slow read before a question goes, and the file goes with that question only", async () => {
    reads.length = 0;
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} attachable />);
    fireEvent.change(screen.getByLabelText(/ask/i, { selector: "textarea" }), { target: { value: "what is on this?" } });
    pick("a.pdf");
    expect(screen.getByTestId("attach-reading")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("button", { name: "Ask" }).closest("form") ?? document.body);
    expect(onSubmit).not.toHaveBeenCalled();
    await finish(0, "a.pdf");
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][2].map((f: Attachment) => f.name)).toEqual(["a.pdf"]);
    // nothing is left to attach to the next question
    expect(screen.queryByText("a.pdf")).toBeNull();
  });

  it("two quick batches both attach, in order, each checked against what is there", async () => {
    reads.length = 0;
    render(<AskBox onSubmit={vi.fn()} attachable />);
    pick("a.pdf");
    pick("b.pdf");
    await waitFor(() => expect(reads).toHaveLength(1));
    await act(async () => undefined);
    expect(reads).toHaveLength(1); // the second waits its turn
    await finish(0, "a.pdf");
    await waitFor(() => expect(reads).toHaveLength(2));
    await finish(1, "b.pdf");
    await waitFor(() => expect(screen.getByText("b.pdf")).toBeInTheDocument());
    expect(screen.getByText("a.pdf")).toBeInTheDocument();
  });

  it("a file removed while another is still being read stays removed", async () => {
    reads.length = 0;
    render(<AskBox onSubmit={vi.fn()} attachable />);
    pick("a.pdf");
    await finish(0, "a.pdf");
    await screen.findByText("a.pdf");
    pick("b.pdf");
    fireEvent.click(screen.getByRole("button", { name: /Remove a\.pdf/ }));
    await finish(1, "b.pdf");
    await screen.findByText("b.pdf");
    expect(screen.queryByText("a.pdf")).toBeNull();
  });
});

describe("question length (WEB-024)", () => {
  it("the box stops at 1000 characters and says so", () => {
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByLabelText(/ask/i, { selector: "textarea" }) as HTMLTextAreaElement;
    expect(box.maxLength).toBe(1000);
    fireEvent.change(box, { target: { value: "a".repeat(950) } });
    expect(screen.getByTestId("question-count")).toHaveTextContent("950 of 1000");
    fireEvent.change(box, { target: { value: "a".repeat(1000) } });
    expect(screen.getByTestId("question-count")).toHaveTextContent("the limit");
  });
});
