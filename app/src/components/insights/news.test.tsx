import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getInsightNews = vi.fn();
const putAwayInsightNews = vi.fn();
let viewing = false;
vi.mock("@/lib/api", () => ({
  getInsightNews: (...a: unknown[]) => getInsightNews(...a),
  putAwayInsightNews: (...a: unknown[]) => putAwayInsightNews(...a),
}));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => viewing }));

import { InsightNewsList } from "./news";

const item = (n: number) => ({ run_id: `r${n}`, insight_id: `s${n}`, title: `Late POs ${n}`, ran_at: "2026-11-19T12:00:00+00:00", row_count: 5, new_rows: 2, said: "2 new rows since it last ran (5 rows now)." });

beforeEach(() => {
  viewing = false;
  getInsightNews.mockReset().mockResolvedValue({ items: [item(1), item(2)] });
  putAwayInsightNews.mockReset().mockResolvedValue(undefined);
});

describe("Home: your reports", () => {
  it("lists what each run found, opens the insight, and puts one away", async () => {
    render(<InsightNewsList />);
    await waitFor(() => expect(screen.getAllByTestId("insight-news-row")).toHaveLength(2));
    expect(screen.getByText("Late POs 1").closest("a")?.getAttribute("href")).toBe("/insights#insight-s1");
    fireEvent.click(screen.getAllByText("Put away")[0]);
    await waitFor(() => expect(screen.getAllByTestId("insight-news-row")).toHaveLength(1));
    expect(putAwayInsightNews).toHaveBeenCalledWith("r1");
  });

  it("offers a monitor's why question as a link to the Ask, and nothing for a plain report", async () => {
    const why = "Why is units sold unusual in the last 30 days against the periods before? Break it down and say what drove it.";
    getInsightNews.mockResolvedValueOnce({ items: [{ ...item(1), ask: why }, { ...item(2), ask: "" }, { ...item(3), ask: "Late orders by customer" }] });
    render(<InsightNewsList />);
    await waitFor(() => expect(screen.getAllByTestId("insight-news-row")).toHaveLength(3));
    const links = screen.getAllByTestId("insight-news-ask");
    expect(links.map((l) => l.textContent)).toEqual(["Ask why", "Ask about it"]);
    expect(links[0].getAttribute("href")).toBe(`/ask?q=${encodeURIComponent(why)}`);
    expect(new URLSearchParams(links[0].getAttribute("href")!.split("?")[1]).get("q")).toBe(why);
  });

  it("shows nothing when none came, when it cannot be read, or while viewing as someone", async () => {
    getInsightNews.mockResolvedValueOnce({ items: [] });
    const { container, unmount } = render(<InsightNewsList />);
    await waitFor(() => expect(getInsightNews).toHaveBeenCalled());
    expect(container.textContent).toBe("");
    unmount();
    getInsightNews.mockRejectedValueOnce(new Error("down"));
    const second = render(<InsightNewsList />);
    await waitFor(() => expect(getInsightNews).toHaveBeenCalledTimes(2));
    expect(second.container.textContent).toBe("");
    second.unmount();
    viewing = true;
    const third = render(<InsightNewsList />);
    expect(third.container.textContent).toBe("");
    expect(getInsightNews).toHaveBeenCalledTimes(2);
  });
});
