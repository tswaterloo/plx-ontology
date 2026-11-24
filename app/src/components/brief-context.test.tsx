import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { Brief } from "@/lib/types";
import { AddContext, askAboutBriefHref, noteTargets } from "./brief-context";

const save = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), saveMemory: save }));

const brief: Brief = {
  kind: "meeting", title: "Sync with Azelis", as_of: "2026-11-24", basis: "b", sections: [],
  subject: { object_id: "CalendarEvent:graph.c1", type: "CalendarEvent", label: "Sync", open_url: "/x" },
  parties: [{ object_id: "Customer:bc.1", type: "Customer", label: "Azelis", open_url: "/c" }],
};

describe("add context on a brief", () => {
  beforeEach(() => save.mockReset());

  it("notes can be about the meeting or each party, and Ask carries the brief", () => {
    expect(noteTargets(brief).map((t) => t.label)).toEqual(["This meeting", "Azelis"]);
    const href = askAboutBriefHref(brief)!;
    expect(href).toContain("brief=CalendarEvent");
    expect(href).toContain("object=CalendarEvent%3Agraph.c1");
  });

  it("saves the note as the person's own fact about the chosen record, then reads the brief again", async () => {
    save.mockResolvedValue({});
    const saved = vi.fn();
    render(<AddContext brief={brief} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText(/Add context/), { target: { value: "They want net 60" } });
    fireEvent.change(screen.getByLabelText("About"), { target: { value: "Customer:bc.1" } });
    fireEvent.click(screen.getByTestId("add-context-save"));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(save).toHaveBeenCalledWith({ kind: "fact", text: "They want net 60", records: ["Customer:bc.1"] });
  });

  it("says why a note was refused and keeps the text", async () => {
    save.mockRejectedValueOnce(new ApiError(422, "memory_refused", undefined, "Memory never keeps pay or finance."));
    render(<AddContext brief={brief} onSaved={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Add context/), { target: { value: "net 60 at $5" } });
    fireEvent.click(screen.getByTestId("add-context-save"));
    expect((await screen.findByRole("alert")).textContent).toBeTruthy();
    expect((screen.getByLabelText(/Add context/) as HTMLTextAreaElement).value).toBe("net 60 at $5");
  });
});
