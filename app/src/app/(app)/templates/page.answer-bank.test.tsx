import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DocList } from "@/lib/types";

const bank = vi.fn<(q?: string) => Promise<DocList>>();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/templates" }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getTemplates: () => Promise.resolve({ available: true, templates: [], doc_types: [], roles: [] }),
  getAnswerBank: (q?: string) => bank(q),
  getLearnedTemplates: () => Promise.resolve({ status: "none", templates: [] }),
}));

import TemplatesPage from "./page";

const row = { title: "Do you hold a GMP certificate?", detail: "Yes, since 2019", dated: "2026-01-05", status: "Any customer", records: [], figures: [], source: { object_id: "Attachment:fx:1", type: "Attachment", label: "Supplier questionnaire.xlsx", open_url: "/object/Attachment/Attachment:fx:1" } };

describe("templates page: the answer bank", () => {
  it("lists past answers with their questionnaire and ranks them against a typed question", async () => {
    bank.mockImplementation((q) => Promise.resolve({ view: "answer_bank", title: "x", rows: q === "nothing like it" ? [] : [row], withheld: 0, complete: true }));
    render(<TemplatesPage />);
    await waitFor(() => expect(screen.getByText("Yes, since 2019")).toBeTruthy());
    expect(screen.getByText("Supplier questionnaire.xlsx").getAttribute("href")).toBe("/object/Attachment/Attachment:fx:1");
    fireEvent.change(screen.getByLabelText("Find answers to a question"), { target: { value: "nothing like it" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    await waitFor(() => expect(screen.getByTestId("answer-bank-empty").textContent).toContain("No past answer shares a word with “nothing like it”."));
    expect(bank).toHaveBeenLastCalledWith("nothing like it");
  });

  it("says when the list could not be read, never an empty list", async () => {
    bank.mockResolvedValue({ view: "answer_bank", title: "x", rows: [], withheld: 0, complete: false, error: "The list could not be read." });
    render(<TemplatesPage />);
    await waitFor(() => expect(screen.getByText("The list could not be read.")).toBeTruthy());
    expect(screen.queryByTestId("answer-bank-empty")).toBeNull();
  });
});
