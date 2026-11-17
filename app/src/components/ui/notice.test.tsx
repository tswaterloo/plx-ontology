import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { plainError, ApiError } from "@/lib/errors";
import { ErrorNotice } from "./notice";

describe("ErrorNotice", () => {
  it("viewing as someone, a page it does not cover is a calm note with no Try again", () => {
    const retry = vi.fn();
    render(<ErrorNotice message={plainError(new ApiError(403, "view_as_refused"))} onRetry={retry} />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("You are viewing as someone else");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("a failed read is still an error with Try again", () => {
    render(<ErrorNotice message={plainError(new ApiError(503, "busy_x"))} onRetry={() => undefined} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
