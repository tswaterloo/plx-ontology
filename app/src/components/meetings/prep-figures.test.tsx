import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BriefSection } from "@/lib/types";
import { PrepSection } from "./meeting-sheet";

const section = (over: Partial<BriefSection>): BriefSection =>
  ({ key: "money", title: "Money with this customer", state: "read", rows: [], more: 0, figures: [], ...over }) as unknown as BriefSection;

describe("meeting prep sheet shows figure-only sections (WEB-033)", () => {
  it("renders each figure's value, change and caveat, and a figure that was not read says so", () => {
    render(
      <PrepSection
        s={section({
          figures: [
            { label: "Open balance", unit: "money", currency: "CAD", value: "1250.50", delta: "+100.00", previous: "1150.50", sources: [] },
            { label: "On-time share", unit: "percent", value: "92", note: "Last 90 days", sources: [] },
            { label: "Credit limit", unit: "money", currency: "CAD", value: null, note: "Could not be read.", sources: [] },
          ],
        })}
      />,
    );
    const figures = screen.getByTestId("prep-figures");
    expect(figures).toHaveTextContent("Open balance");
    expect(figures).toHaveTextContent("1,250.50");
    expect(figures).toHaveTextContent("vs");
    expect(figures).toHaveTextContent("92%");
    expect(figures).toHaveTextContent("Last 90 days");
    expect(figures).toHaveTextContent("Credit limit");
    expect(figures).toHaveTextContent("Not read");
  });

  it("a mixed section keeps its rows beside its figures; a rows-only section draws no figure list", () => {
    const row = { label: "PO 100", sources: [] };
    const { rerender } = render(<PrepSection s={section({ rows: [row], figures: [{ label: "Late", unit: "records", value: "3", sources: [] }] } as never)} />);
    expect(screen.getByTestId("prep-figures")).toHaveTextContent("3");
    expect(screen.getByText("PO 100")).toBeInTheDocument();
    rerender(<PrepSection s={section({ rows: [row] } as never)} />);
    expect(screen.queryByTestId("prep-figures")).toBeNull();
  });
});
