import { describe, expect, it } from "vitest";
import { splitSignature } from "./source-view";

describe("splitSignature (WEB-029): business instructions are never folded away", () => {
  it("keeps short instruction lines that sit beside an email address, with the real signature folded", () => {
    const body = [
      "Please arrange the shipment.",
      "Hold all stock pending approval.",
      "buyer@example.invalid",
      "Release only after QA signs.",
      "The shipment must not leave today.",
      "",
      "Dana Price",
      "Purchasing, PLX",
      "dana@plx.example",
      "T: 416 555 0147",
    ].join("\n");
    const { main, signature } = splitSignature(body);
    for (const kept of ["Hold all stock pending approval.", "Release only after QA signs.", "The shipment must not leave today."]) expect(main).toContain(kept);
    expect(signature).toContain("Dana Price");
    expect(signature).not.toContain("Release only after QA signs.");
  });

  it("with no signature at all, nothing is folded", () => {
    const body = "Hold all stock.\nbuyer@example.invalid\nRelease only after QA signs.";
    expect(splitSignature(body)).toEqual({ main: body, signature: "" });
  });

  it("a sign-off folds what follows it, and a mid-message Thanks does not fold the rest of the message", () => {
    const real = "Shipment is held.\n\nThanks,\nDana\ndana@plx.example\n416 555 0147";
    expect(splitSignature(real).signature).toBe("Thanks,\nDana\ndana@plx.example\n416 555 0147");
    const mid = ["Thanks", "Now the part that matters: do not release lot 4410 until the QA team has signed the certificate of analysis and sent it to the customer.", "Call me on 416 555 0147 if anything changes."].join("\n");
    expect(splitSignature(mid)).toEqual({ main: mid, signature: "" });
  });

  it("a contact block followed by more message is not a signature", () => {
    const body = ["See below.", "", "Dana Price", "dana@plx.example", "416 555 0147", "", "Please also confirm that the revised delivery date works for the plant before Friday noon."].join("\n");
    expect(splitSignature(body).signature).toBe("");
  });
});
