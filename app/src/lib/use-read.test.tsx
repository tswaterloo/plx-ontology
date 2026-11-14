import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { resetSessionCaches } from "./session-cache";
import { readAhead, useRead } from "./use-read";

function Page({ load }: { load: (s: AbortSignal) => Promise<string> }) {
  const { data, error } = useRead<string>("page", load);
  return <p>{error ? "problem" : (data ?? "reading")}</p>;
}

function later() {
  let resolve!: (v: string) => void;
  const p = new Promise<string>((r) => (resolve = r));
  return { p, resolve };
}

describe("useRead (stale while revalidate)", () => {
  it("paints what the page showed last time at once, then the new read", async () => {
    const first = render(<Page load={() => Promise.resolve("v1")} />);
    expect(await screen.findByText("v1")).toBeInTheDocument();
    first.unmount();
    const next = later();
    render(<Page load={() => next.p} />);
    expect(screen.getByText("v1")).toBeInTheDocument();
    await act(async () => next.resolve("v2"));
    expect(screen.getByText("v2")).toBeInTheDocument();
  });

  it("shows a failed read as a problem even with a kept value", async () => {
    const first = render(<Page load={() => Promise.resolve("v1")} />);
    await screen.findByText("v1");
    first.unmount();
    render(<Page load={() => Promise.reject(new Error("x"))} />);
    expect(await screen.findByText("problem")).toBeInTheDocument();
  });

  it("keeps nothing past a sign-out", async () => {
    const first = render(<Page load={() => Promise.resolve("v1")} />);
    await screen.findByText("v1");
    first.unmount();
    resetSessionCaches();
    render(<Page load={() => new Promise<string>(() => undefined)} />);
    expect(screen.getByText("reading")).toBeInTheDocument();
  });

  it("shows a value read ahead without reading it again", async () => {
    const ahead = vi.fn(() => Promise.resolve("early"));
    readAhead("page", ahead);
    readAhead("page", ahead);
    expect(ahead).toHaveBeenCalledTimes(1);
    await act(async () => undefined);
    const load = vi.fn(() => Promise.resolve("late"));
    render(<Page load={load} />);
    expect(screen.getByText("early")).toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
  });

  it("waits on a read ahead still under way rather than reading twice", async () => {
    const next = later();
    readAhead("page", () => next.p);
    const load = vi.fn(() => Promise.resolve("second"));
    render(<Page load={load} />);
    expect(screen.getByText("reading")).toBeInTheDocument();
    await act(async () => next.resolve("first"));
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
  });
});
