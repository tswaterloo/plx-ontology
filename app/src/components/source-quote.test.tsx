import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { SourceView } from "@/lib/types";
import { quoteOccurs, SourceDocument } from "./source-view";

const view: SourceView = {
  handle: "graph-mail://box/m1@c1", system: "Email", label: "Email message", kind: "record", title: "PO 5275",
  text: "Please release lot 4410 on Friday.\nThe   QA hold is lifted.", fields: { "row.from": "ross@example.com" },
  as_of: null, text_offset: 0, next_offset: null, changed_since: false, access_label: null,
};

describe("a quote in the link (WEB-031)", () => {
  it("a passage that is in the source is shown as the cited passage", () => {
    render(<SourceDocument view={view} text={view.text!} quote="the QA hold is lifted" />);
    expect(screen.getByTestId("cited-passage")).toHaveTextContent("Cited passage");
  });

  it("a passage that is not in the source is labelled as the link's own words, never as cited", () => {
    render(<SourceDocument view={view} text={view.text!} quote="We agree to pay double." />);
    const figure = screen.getByTestId("cited-passage");
    expect(figure).toHaveTextContent("Quoted in the link (not found in this source)");
    expect(figure).not.toHaveTextContent("Cited passage");
  });

  it("matches across spacing, case and smart quotes, and an empty quote matches nothing", () => {
    expect(quoteOccurs("lot 4410  ON friday", view.text!)).toBe(true);
    expect(quoteOccurs("it’s fine", "It's fine")).toBe(true);
    expect(quoteOccurs("", view.text!)).toBe(false);
    expect(quoteOccurs("ross@example.com", "", view.fields)).toBe(true);
  });
});
