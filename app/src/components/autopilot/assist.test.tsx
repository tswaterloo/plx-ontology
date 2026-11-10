import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistDraft } from "@/lib/types";

const api = vi.hoisted(() => ({ assist: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), assistDraft: api.assist }));

import { ApiError } from "@/lib/errors";
import { AssistBox, mailtoOf } from "./assist";

const draft = (over: Partial<AssistDraft> = {}): AssistDraft => ({
  intent: "tell Sam the PO 90412 is late", channel: "mail",
  to: [{ address: "sam@plx.example", name: "Sam" }], choices: [], withheld: 0,
  subject: "PO 90412 is late", body: "Hi Sam,\n\nThe PO 90412 is late.\n- PO 90412 was expected on 17 Oct.",
  facts: [{ text: "PO 90412 was expected on 17 Oct.", sources: [{ system: "BC", handle: "bc://co/po/90412", label: "BC purchase order 90412" }] }],
  gaps: [], questions: ["the PO 90412 is late"], sent: false, ...over,
});

describe("Assist", () => {
  beforeEach(() => api.assist.mockReset());

  it("drafts the typed line: recipients, subject, editable body, each fact with its source; nothing is sent", async () => {
    api.assist.mockResolvedValue(draft());
    render(<AssistBox />);
    fireEvent.change(screen.getByLabelText("What should be sent, and to whom"), { target: { value: "tell Sam the PO 90412 is late" } });
    fireEvent.click(screen.getByRole("button", { name: "Draft" }));
    await screen.findByTestId("assist-draft");
    expect(api.assist).toHaveBeenCalledWith("tell Sam the PO 90412 is late");
    expect(screen.getByText("sam@plx.example")).toBeTruthy();
    expect(screen.getByText(/Nothing is sent/)).toBeTruthy();
    expect((screen.getByLabelText("Message") as HTMLTextAreaElement).value).toContain("The PO 90412 is late.");
    expect(screen.getByRole("link", { name: "BC purchase order 90412" }).getAttribute("href")).toBe("/source?handle=bc%3A%2F%2Fco%2Fpo%2F90412");
    const open = screen.getByRole("link", { name: "Open in my mail" }).getAttribute("href") ?? "";
    expect(open.startsWith("mailto:sam@plx.example?subject=PO%2090412%20is%20late&body=")).toBe(true);
  });

  it("shows choices, people left out and gaps, and offers no mail link without a recipient", async () => {
    api.assist.mockResolvedValue(draft({ to: [], withheld: 1, choices: [{ mention: "Sam", options: [{ address: "sam@plx.example" }, { address: "sam.o@plx.example" }] }], gaps: ["\"Sam\" matches several people: pick one"] }));
    render(<AssistBox />);
    fireEvent.change(screen.getByLabelText("What should be sent, and to whom"), { target: { value: "tell Sam" } });
    fireEvent.click(screen.getByRole("button", { name: "Draft" }));
    await screen.findByTestId("assist-draft");
    expect(screen.getByText(/could be:/)).toBeTruthy();
    expect(screen.getByText(/left out/)).toBeTruthy();
    expect(screen.getByText(/pick one/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Open in my mail" })).toBeNull();
  });

  it("says a refusal in plain words", async () => {
    api.assist.mockRejectedValueOnce(new ApiError(503, "assist_refused", undefined, "Drafting is paused: the people list it checks recipients against is not loaded."));
    render(<AssistBox />);
    fireEvent.change(screen.getByLabelText("What should be sent, and to whom"), { target: { value: "tell Sam" } });
    fireEvent.click(screen.getByRole("button", { name: "Draft" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Drafting is paused"));
    expect(screen.queryByTestId("assist-draft")).toBeNull();
  });

  it("builds a mail link that opens the person's own client", () => {
    expect(mailtoOf(draft(), "a b")).toBe("mailto:sam@plx.example?subject=PO%2090412%20is%20late&body=a%20b");
  });
});
