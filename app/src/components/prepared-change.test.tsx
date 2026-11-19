import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PreparedWrite } from "@/lib/types";
import { PreparedChanges } from "./prepared-change";

const api = vi.hoisted(() => ({ getPrepared: vi.fn(), preparedStep: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<object>()),
  ...api,
}));

const card = (over: Partial<PreparedWrite> = {}): PreparedWrite => ({
  prepared_id: "pw_1",
  state: "prepared",
  origin: "instruction",
  title: "Chase the carrier",
  reason: "You asked for a task.",
  citations: [],
  actions: [
    {
      kind: "planner_write",
      system: "planner",
      operation: "task_create",
      target: {},
    },
  ],
  diff: [{ field: "title", before: null, after: "Chase the carrier" }],
  undo: ["It can be undone from here."],
  outcome: [],
  live: true,
  missing: [],
  acknowledge: [],
  preview_hash: "h".repeat(64),
  ...over,
});

describe("prepared change in an Ask answer", () => {
  beforeEach(() => {
    api.getPrepared.mockReset();
    api.preparedStep.mockReset();
  });

  it("shows before/after, confirms only on the second click with the preview's hash, then offers undo", async () => {
    api.getPrepared.mockResolvedValue(card());
    api.preparedStep.mockResolvedValue(
      card({
        state: "confirmed",
        preview_hash: null,
        outcome: [
          {
            kind: "planner_write",
            status: "done",
            mode: "live",
            reasons: [],
            key: "k",
          },
        ],
      }),
    );
    render(<PreparedChanges ids={["pw_1"]} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Review change" }),
    );
    expect(screen.getByTestId("prepared-diff").textContent).toContain(
      "(empty)",
    );
    expect(screen.getByTestId("prepared-diff").textContent).toContain(
      "Chase the carrier",
    );
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(api.preparedStep).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm: do it now" }),
    );
    expect(api.preparedStep).toHaveBeenCalledWith(
      "pw_1",
      "confirm",
      "h".repeat(64),
    );
    expect(screen.getByTestId("prepared-state").textContent).toBe("Done.");
    expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy();
  });

  it("says why a refused confirm was refused and writes nothing on dismiss", async () => {
    api.getPrepared.mockResolvedValue(
      card({
        live: false,
        missing: ["planner is not opened to approved writes."],
      }),
    );
    api.preparedStep.mockResolvedValue(
      card({ state: "dismissed", preview_hash: null }),
    );
    render(<PreparedChanges ids={["pw_1"]} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Review change" }),
    );
    expect(
      screen.getByText("planner is not opened to approved writes."),
    ).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(api.preparedStep).toHaveBeenCalledWith("pw_1", "dismiss");
    expect(screen.getByTestId("prepared-state").textContent).toBe(
      "Dismissed; nothing was written.",
    );
  });

  it("shows the system, the record and a message whole: recipients, subject and the full body", async () => {
    api.getPrepared.mockResolvedValue(
      card({
        actions: [
          {
            kind: "send_mail",
            system: "graph",
            operation: "send_email",
            target: { sender: "ana@plx.example" },
            record: null,
            fields: {},
            people: [],
            message: {
              sender: "ana@plx.example",
              to: ["buyer@customer.example"],
              cc: ["lee@plx.example"],
              subject: "PO 7",
              body: "Line one.\nLine two.",
            },
          },
          {
            kind: "planner_write",
            system: "planner",
            operation: "task_update",
            target: { task_id: "task-0001" },
            record: "Chase the carrier",
            fields: { title: "New" },
            people: [],
          },
          {
            kind: "bc_write",
            system: "bc",
            operation: "sales_order_update",
            target: { company_id: "c-2", document_id: "d-1" },
            record: "Sales order SO-1",
            company: { id: "c-2", name: "Petra Soap" },
            fields: { requestedDeliveryDate: "2026-11-27" },
            people: [],
            condition: { etag: 'W/"bc1"', status: "Open" },
          },
        ],
      }),
    );
    api.preparedStep.mockResolvedValue(
      card({ state: "dismissed", preview_hash: null }),
    );
    render(<PreparedChanges ids={["pw_1"]} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Review change" }),
    );
    const message = screen.getByTestId("prepared-message").textContent ?? "";
    for (const part of [
      "ana@plx.example",
      "buyer@customer.example",
      "lee@plx.example",
      "PO 7",
      "Line one.",
      "Line two.",
    ])
      expect(message).toContain(part);
    const [, task] = screen.getAllByTestId("prepared-action");
    expect(task.textContent).toContain("Planner");
    expect(task.textContent).toContain("Chase the carrier (task_id task-0001)");
    expect(task.textContent).toContain("title: New");
    // the BC company written is shown by BC's name and its id, with the condition
    const order = screen.getAllByTestId("prepared-action")[2].textContent ?? "";
    expect(order).toContain("Company: Petra Soap (c-2)");
    expect(order).toContain('Only if: etag W/"bc1", status Open');
  });

  it("offers to prepare again a dismissed change, then works on the new item", async () => {
    api.getPrepared.mockResolvedValueOnce(
      card({ state: "dismissed", preview_hash: null }),
    );
    api.preparedStep.mockResolvedValueOnce(
      card({ prepared_id: "pw_2", preview_hash: null }),
    );
    api.getPrepared.mockResolvedValueOnce(card({ prepared_id: "pw_2" }));
    render(<PreparedChanges ids={["pw_1"]} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Review change" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Prepare again" }),
    );
    expect(api.preparedStep).toHaveBeenCalledWith("pw_1", "retry");
    expect(api.getPrepared).toHaveBeenLastCalledWith("pw_2");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(api.preparedStep).toHaveBeenLastCalledWith("pw_2", "dismiss");
  });
});
