import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AutopilotArea } from "@/lib/types";

const who = vi.hoisted(() => ({ owner: true }));
vi.mock("@/components/auth", () => ({ useAuth: () => ({ state: { status: "signed-in", me: { owner: who.owner } } }) }));

import { DraftsPanel } from "./opt-in";

const area = { my_drafts: [{ choice: "customer_replies", label: "Replies to customers", on: true, by_owner: true }] } as unknown as AutopilotArea;

describe("Switches as the owner reads them", () => {
  it("never tells the owner what the owner set for them", () => {
    render(<DraftsPanel area={area} onArea={() => undefined} />);
    expect(screen.getByTestId("draft-choice-state").textContent).toBe("On. Prepared for you; waits for your confirm.");
    expect(screen.queryByText(/what the owner turned on for you/)).toBeNull();
  });

  it("tells anyone else who turned it on", () => {
    who.owner = false;
    render(<DraftsPanel area={area} onArea={() => undefined} />);
    expect(screen.getAllByTestId("draft-choice-state").at(-1)!.textContent).toBe("On, set by the owner. Prepared for you; waits for your confirm.");
    expect(screen.getByText(/what the owner turned on for you/)).toBeTruthy();
  });
});
