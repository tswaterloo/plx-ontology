import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { withPreset } from "@/lib/actions";
import type { ActionPreview, AskDraft } from "@/lib/types";
import { AskDrafts } from "./ask-drafts";

const api = vi.hoisted(() => ({ previewAction: vi.fn(), confirmAction: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));

const draft = (over: Partial<AskDraft> = {}): AskDraft => ({
  id: "ask:a1:0",
  kind: "email",
  title: "Cabinet",
  email_subject: "Cabinet",
  body: "Hi,\nAbout the cabinet.",
  details: [],
  actions: [{ kind: "send_email", label: "Send email", outbound: true }] as AskDraft["actions"],
  missing: [],
  also_to: [],
  cc: [],
  recipient_questions: [
    { field: "to", pick: "one", label: 'Who is "Maple"? Your mail does not settle it', candidates: ["pat@maple.example", "kim@maple.example"] },
    { field: "cc", pick: "any", label: "Also copy? You often copy them on mail like this", candidates: ["ben@plx.example"] },
  ],
  record_questions: [{ record: "77777", outcome: "not_found", note: "No record of 77777 found in the records you can read.", ask_line: "Could you confirm the details of 77777?" }],
  sent: false,
  ...over,
});

const preview = (payload: ActionPreview["payload"]): ActionPreview => ({
  card_id: "ask:a1:0",
  kind: "send_email",
  label: "Send email",
  outbound: true,
  sender: "me@plx.example",
  payload,
  payload_hash: "a".repeat(64),
  citations: [],
  uncited: [],
  unknown_domains: [],
  problems: [],
  missing: [],
  request: null,
  teams_chats: [],
});

describe("withPreset", () => {
  it("adds each pick once, To before Cc, and each line once at the end", () => {
    const got = withPreset({ to: ["A@x.example"], cc: ["b@x.example"], body: "Hi\n" }, { to: ["a@x.example", "b@x.example"], cc: ["c@x.example"], append: ["Q?", "Q?"] });
    expect(got.to).toEqual(["A@x.example", "b@x.example"]);
    expect(got.cc).toEqual(["c@x.example"]);
    expect(got.body).toBe("Hi\n\nQ?");
  });
});

describe("AskDrafts questions", () => {
  it("one tap picks who it is for, who else, and asks the recipient; the send preview carries the picks", async () => {
    api.previewAction.mockReset();
    api.previewAction.mockImplementation(({ payload }: { payload?: ActionPreview["payload"] }) =>
      Promise.resolve(preview(payload ?? { to: [], cc: [], subject: "Cabinet", body: "Hi,\nAbout the cabinet.", in_reply_to: null })),
    );
    render(<AskDrafts drafts={[draft()]} />);
    const picks = screen.getAllByTestId("draft-pick");
    expect(picks.map((b) => b.textContent)).toEqual(["pat@maple.example", "kim@maple.example", "ben@plx.example"]);
    fireEvent.click(picks[1]);
    fireEvent.click(picks[0]); // pick one: the last tap wins
    fireEvent.click(picks[2]);
    fireEvent.click(screen.getByTestId("draft-ask-recipient"));
    expect(picks[0]).toHaveAttribute("aria-pressed", "true");
    expect(picks[1]).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByTestId("action-send_email"));
    await waitFor(() => expect(api.previewAction).toHaveBeenCalledTimes(2));
    // the confirm waits for the preview that holds the picks
    await waitFor(() => expect(screen.getByTestId("action-confirm")).not.toBeDisabled());
    const sent = api.previewAction.mock.calls[1][0].payload;
    expect(sent.to).toEqual(["pat@maple.example"]);
    expect(sent.cc).toEqual(["ben@plx.example"]);
    expect(sent.body).toContain("Could you confirm the details of 77777?");
  });

  it("a draft with nothing open shows no questions and opens its preview as drafted", async () => {
    api.previewAction.mockReset();
    api.previewAction.mockResolvedValue(preview({ to: ["pat@maple.example"], cc: [], subject: "Cabinet", body: "Hi", in_reply_to: null }));
    render(<AskDrafts drafts={[draft({ to: "pat@maple.example", recipient_questions: [], record_questions: [] })]} />);
    expect(screen.queryByTestId("draft-questions")).toBeNull();
    expect(screen.getByTestId("draft-recipients")).toHaveTextContent("To pat@maple.example");
    fireEvent.click(screen.getByTestId("action-send_email"));
    await waitFor(() => expect(api.previewAction).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(api.previewAction).toHaveBeenCalledTimes(1);
  });
});
