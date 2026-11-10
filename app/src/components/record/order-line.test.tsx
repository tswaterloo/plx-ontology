import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { businessToday } from "@/lib/format";
import { OrderLine } from "./parts";

afterEach(() => vi.useRealTimers());

describe("an order's line", () => {
  it("names today as the business's day: 8 p.m. in Toronto is not yet tomorrow", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-10T01:00:00Z")); // 8 p.m. 9 Nov in Toronto
    expect(businessToday()).toBe("2026-11-09");
    render(<OrderLine ordered={Date.parse("2026-09-26T12:00:00Z")} needed={Date.parse("2026-10-17T12:00:00Z")} />);
    const line = screen.getByTestId("order-line");
    expect(line).toHaveTextContent("Today9 Nov");
    expect(line).not.toHaveTextContent("10 Nov");
  });
});
