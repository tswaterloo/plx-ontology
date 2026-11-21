import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// a page opened fresh (not Home) has no Home mounted to publish the count
async function freshShell() {
  vi.resetModules();
  return import("./shell");
}

function Probe({ use }: { use: () => number | null }) {
  const n = use();
  return <span data-testid="n">{n === null ? "unknown" : String(n)}</span>;
}

describe("Home's count on every page", () => {
  it("shows Home's last full count on a page opened after it, for half an hour", async () => {
    window.sessionStorage.setItem("plx.needs-count", JSON.stringify({ n: 58, at: Date.now() - 60_000 }));
    const shell = await freshShell();
    render(<Probe use={shell.useNeedsCount} />);
    expect(screen.getByTestId("n").textContent).toBe("58");
  });

  it("never a count older than that, and none after the person signs out or changes", async () => {
    window.sessionStorage.setItem("plx.needs-count", JSON.stringify({ n: 58, at: Date.now() - 31 * 60_000 }));
    let shell = await freshShell();
    const { unmount } = render(<Probe use={shell.useNeedsCount} />);
    expect(screen.getByTestId("n").textContent).toBe("unknown");
    unmount();
    window.sessionStorage.setItem("plx.needs-count", JSON.stringify({ n: 58, at: Date.now() }));
    shell = await freshShell();
    shell.publishNeedsCount(null, true);
    shell = await freshShell();
    render(<Probe use={shell.useNeedsCount} />);
    expect(screen.getByTestId("n").textContent).toBe("unknown");
  });
});
