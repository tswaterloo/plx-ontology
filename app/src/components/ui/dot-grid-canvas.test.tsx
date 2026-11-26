import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DotGridCanvas, computeFitViewport, edgeCurve, type DotGridEdge, type DotGridNode } from "./dot-grid-canvas";

const sampleNodes: DotGridNode[] = [
  {
    id: "trigger",
    x: 40,
    y: 80,
    kindLabel: "Trigger",
    title: "Late PO detected",
    description: "Runs when a confirmed delivery slips past due date.",
    badge: "Live",
    tone: "accent",
  },
  {
    id: "approval",
    x: 360,
    y: 80,
    kindLabel: "Gate",
    title: "Buyer approval",
    description: "Routes draft follow-up to the assigned buyer.",
    badge: "Required",
    tone: "ok",
  },
];

const sampleEdges: DotGridEdge[] = [
  { id: "e1", from: "trigger", to: "approval", label: "Slip > 2d" },
];

describe("DotGridCanvas", () => {
  it("renders dot-matrix SVG pattern, nodes, cubic Bezier edges, and selection state", () => {
    const onSelect = vi.fn();
    render(
      <DotGridCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        selectedId="trigger"
        onSelectNode={onSelect}
        ariaLabel="Workflow canvas"
      />,
    );

    const canvas = screen.getByRole("region", { name: "Workflow canvas" });
    expect(canvas.querySelector("pattern circle")).toBeTruthy();

    const triggerBtn = screen.getByRole("button", { name: /Late PO detected/i });
    const approvalBtn = screen.getByRole("button", { name: /Buyer approval/i });
    expect(triggerBtn.getAttribute("aria-pressed")).toBe("true");
    expect(approvalBtn.getAttribute("aria-pressed")).toBe("false");

    const edgesLayer = screen.getByTestId("dot-grid-edges");
    const edgeGroup = edgesLayer.querySelector('[data-edge-id="e1"]');
    expect(edgeGroup?.getAttribute("data-active")).toBe("true");
    expect(edgeGroup?.querySelector("path")?.getAttribute("d")).toMatch(/^M .+ C .+/);
    expect(edgesLayer.textContent).toContain("Slip > 2d");

    fireEvent.click(approvalBtn);
    expect(onSelect).toHaveBeenCalledWith("approval");
  });

  it("supports zoom in, zoom out, fit view, and pointer pan on background", () => {
    render(<DotGridCanvas nodes={sampleNodes} edges={sampleEdges} />);
    const canvas = screen.getByTestId("dot-grid-canvas");
    const dock = screen.getByTestId("dot-grid-dock");
    expect(dock.className).toContain("liquid-glass-dock");

    const initialZoom = Number(canvas.getAttribute("data-zoom"));
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    const zoomedIn = Number(canvas.getAttribute("data-zoom"));
    expect(zoomedIn).toBeGreaterThan(initialZoom);

    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    const zoomedOut = Number(canvas.getAttribute("data-zoom"));
    expect(zoomedOut).toBeLessThan(initialZoom);

    const beforeX = Number(canvas.getAttribute("data-pan-x"));
    fireEvent.pointerDown(canvas, { button: 0, pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 160, clientY: 145 });
    fireEvent.pointerUp(canvas, { pointerId: 1, clientX: 160, clientY: 145 });
    expect(Number(canvas.getAttribute("data-pan-x"))).toBe(beforeX + 60);

    fireEvent.click(screen.getByRole("button", { name: "Fit view" }));
    expect(Number(canvas.getAttribute("data-zoom"))).toBeCloseTo(initialZoom, 2);
  });

  it("shows emptyMessage when no nodes exist", () => {
    render(<DotGridCanvas nodes={[]} edges={[]} emptyMessage="No objects linked yet." />);
    expect(screen.getByText("No objects linked yet.")).toBeTruthy();
  });

  it("computes fit viewport and cubic Bezier curves cleanly", () => {
    const fit = computeFitViewport(sampleNodes, 800, 480);
    expect(fit.zoom).toBeGreaterThan(0.4);
    expect(fit.zoom).toBeLessThanOrEqual(1);
    const curve = edgeCurve(sampleNodes[0], sampleNodes[1]);
    expect(curve.path).toContain("M 276 132 C ");
    expect(curve.midX).toBeCloseTo(318, 0);
    expect(curve.midY).toBeCloseTo(132, 0);
  });
});
