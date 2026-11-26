import { describe, expect, it } from "vitest";
import { cleanHeadline, formatSender, noteParts, saidOnce, threadTopicKey } from "./lines";

describe("A card says each thing once", () => {
  it("leaves out the quote and next step that repeat the summary, keeps what adds to it", () => {
    const s = "Answer which months, November and December 2026, are acceptable";
    expect(saidOnce([s, s, `"${s}."`])).toEqual([s, "", ""]);
    expect(saidOnce(["Confirm PO 5", "Can you confirm PO 5 by Friday?"])).toEqual(["Confirm PO 5", "Can you confirm PO 5 by Friday?"]);
  });
  it("splits the quiet label from the note's own sentence", () => {
    expect(noteParts("Answer which months are acceptable\nThis is on Venessa's list, not yours. Only you can see this.")).toEqual({
      text: "Answer which months are acceptable",
      labels: ["This is on Venessa's list, not yours. Only you can see this."],
    });
    expect(noteParts("\nThis is on another team member's list, not yours. Only you can see this.")).toEqual({
      text: "",
      labels: ["This is on another team member's list, not yours. Only you can see this."],
    });
    expect(noteParts("")).toEqual({ text: "", labels: [] });
  });
  it("repairs legacy missing spaces and separates inline privacy-scope tags", () => {
    expect(noteParts("Draft on request.\nThis is on Alex's list,not yours.Only you can see this.")).toEqual({
      text: "Draft on request.",
      labels: ["This is on Alex's list, not yours. Only you can see this."],
    });
    expect(noteParts("Mission Control task TASK-3601 Only you can see this.")).toEqual({
      text: "Mission Control task TASK-3601",
      labels: ["Only you can see this."],
    });
    expect(noteParts("QC hold or fail This is on Pat's list,not yours.Only you can see this.")).toEqual({
      text: "QC hold or fail",
      labels: ["This is on Pat's list, not yours. Only you can see this."],
    });
  });
  it("cleans RE:/Fwd: prefixes and robotic lead-ins and formats senders cleanly", () => {
    expect(cleanHeadline("RE: Fwd: Workbench instant Teams updates")).toBe("Workbench instant Teams updates");
    expect(cleanHeadline("You said: Send updated pricing sheet")).toBe("Send updated pricing sheet");
    expect(cleanHeadline("No answer yet from Ricardo: PO 10442 ETA")).toBe("PO 10442 ETA");
    expect(threadTopicKey("RE: Workbench instant Teams updates")).toBe(threadTopicKey("Workbench instant Teams updates"));
    expect(formatSender("Taylor Valton <taylorvalton@gmail.com>")).toEqual({ name: "Taylor Valton", email: "taylorvalton@gmail.com" });
    expect(formatSender("dana.buyer@customer.example")).toEqual({ name: "Dana Buyer", email: "dana.buyer@customer.example" });
    expect(formatSender("taylorvalton@gmail.com")).toEqual({ name: "taylorvalton", email: "taylorvalton@gmail.com" });
  });
});
