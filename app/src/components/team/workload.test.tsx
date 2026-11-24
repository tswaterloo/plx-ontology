import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { forgetKeptReads } from "@/lib/use-read";
import { ApiError } from "@/lib/errors";
import { grantsOf, teamCode, teamName, WorkloadSection } from "./workload";

const api = vi.hoisted(() => ({ getWorkload: vi.fn(), getWorkloadGrants: vi.fn(), setWorkloadGrants: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));
const view = vi.hoisted(() => ({ as: null as null | { id: string } }));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => view.as }));

const A = "00000000-0000-4000-8000-0000000000a1";
const B = "00000000-0000-4000-8000-0000000000b1";
const C = "00000000-0000-4000-8000-0000000000b2";

beforeEach(() => {
  forgetKeptReads();
  Object.values(api).forEach((f) => f.mockReset());
  view.as = null;
  api.getWorkload.mockResolvedValue({
    teams: [{ team: "SALES", awaiting_reply: null, tasks: 3, alerts: 1, suppressed: true }, { team: "not_assigned", awaiting_reply: 5, tasks: 0, alerts: 0, suppressed: false }],
    people: [{ person: A, name: "Avery Lindqvist", team: "SALES", self: true, awaiting_reply: 2, tasks: 1, alerts: 0 }, { person: B, name: "Bea Okafor", team: "SALES", self: false, awaiting_reply: 4, tasks: 0, alerts: 0 }],
    granted_teams: ["SALES"],
  });
  api.getWorkloadGrants.mockResolvedValue({
    people: [{ oid: A, name: "Avery Lindqvist", team: "SALES", sees: [], in_snapshot: true }, { oid: B, name: "Bea Okafor", team: null, sees: ["SALES"], in_snapshot: true }, { oid: C, name: null, team: null, sees: [], in_snapshot: false }],
    teams: ["SALES"],
    held_back: 2,
  });
});

describe("teamName", () => {
  it("says a team code in words and keeps an initialism's capitals", () => {
    expect(["CUSTOMER_SERVICE", "QA", "QC", "WH", "SALES", "PROC", "not_assigned"].map(teamName)).toEqual(["Customer service", "QA", "QC", "WH", "Sales", "Proc", "No team yet"]);
  });
});

describe("Team > Workload", () => {
  it("counts per team and per granted person by name, never an object id; a small team shows no reply total", async () => {
    render(<WorkloadSection owner={false} />);
    const teams = await screen.findByTestId("workload-teams");
    expect(within(teams).getAllByTestId("workload-team").map((r) => r.textContent)).toEqual(["Sales·31", "No team yet500"]);
    const people = screen.getByTestId("workload-people");
    expect(people).toHaveTextContent("You");
    expect(people).toHaveTextContent("Bea Okafor");
    expect(people).not.toHaveTextContent(B);
    expect(screen.getByText("You and Sales")).toBeInTheDocument();
    expect(api.getWorkloadGrants).not.toHaveBeenCalled();
    expect(screen.queryByTestId("grant-editor")).toBeNull();
  });

  it("the owner changes a team and a grant; the saved body is the whole sheet", async () => {
    api.setWorkloadGrants.mockResolvedValue({ teams: {}, managers: {} });
    render(<WorkloadSection owner />);
    expect(await screen.findByTestId("grant-editor")).toHaveTextContent("1 of 3 people on a team; 1 see a team person by person.");
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    expect(screen.getByTestId("grant-sheet")).toHaveTextContent("2 entries for them stay as they are.");
    expect(screen.getByText(/Not in the team list \(00000000\)/)).toBeInTheDocument();
    const save = screen.getByTestId("grant-save");
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("New team"), { target: { value: "quality lab" } });
    fireEvent.click(screen.getByRole("button", { name: "Add team" }));
    fireEvent.change(screen.getByLabelText("Team for Bea Okafor"), { target: { value: "QUALITY_LAB" } });
    fireEvent.click(screen.getByLabelText("Avery Lindqvist sees Quality lab"));
    fireEvent.click(save);
    await waitFor(() =>
      expect(api.setWorkloadGrants).toHaveBeenCalledWith({ teams: { [A]: "SALES", [B]: "QUALITY_LAB" }, managers: { [A]: ["QUALITY_LAB"], [B]: ["SALES"] } }),
    );
    await waitFor(() => expect(api.getWorkloadGrants).toHaveBeenCalledTimes(2));
  });

  it("a refusal shows the server's words and nothing is called saved", async () => {
    api.setWorkloadGrants.mockRejectedValue(new ApiError(503, "people_exclusion_unavailable", undefined, "The list of people the Workbench leaves out isn't loaded. Nothing was changed."));
    render(<WorkloadSection owner />);
    fireEvent.click(await screen.findByRole("button", { name: "Change" }));
    fireEvent.change(screen.getByLabelText("Team for Bea Okafor"), { target: { value: "SALES" } });
    fireEvent.click(screen.getByTestId("grant-save"));
    expect(await screen.findByText(/Nothing was changed/)).toBeInTheDocument();
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("viewing as someone shows their counts and never the owner's sheet", async () => {
    view.as = { id: "bea" };
    render(<WorkloadSection owner />);
    await screen.findByTestId("workload-teams");
    expect(api.getWorkloadGrants).not.toHaveBeenCalled();
    expect(screen.queryByTestId("grant-editor")).toBeNull();
  });

  it("a team name is kept as the server's code or refused", () => {
    expect(teamCode(" quality lab ")).toBe("QUALITY_LAB");
    expect(teamCode("9 lives")).toBeNull();
    expect(teamCode("x".repeat(40))).toBeNull();
    expect(grantsOf({ [A]: { team: "", sees: [] }, [B]: { team: "OPS", sees: ["SALES", "SALES"] } })).toEqual({ teams: { [B]: "OPS" }, managers: { [B]: ["SALES"] } });
  });
});
