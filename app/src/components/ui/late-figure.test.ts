import { describe, expect, it } from "vitest";
import { lateEdge, lateFigure } from "./glyphs";

describe("late figure and edge", () => {
  it("is amber up to 30 days and red beyond, in Geist Mono, with a 3 px edge", () => {
    expect(lateFigure(30)).toBe("font-mark text-risk");
    expect(lateFigure(31)).toBe("font-mark text-late");
    expect(lateEdge(30)).toBe("border-l-[3px] border-risk");
    expect(lateEdge(31)).toBe("border-l-[3px] border-late");
  });
});
