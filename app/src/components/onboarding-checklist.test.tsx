import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { checklistKey } from "@/lib/onboarding";
import { OnboardingChecklist } from "./onboarding-checklist";
import { RoleGuide } from "./role-guide";
import { guideFor } from "@/lib/onboarding";

describe("the onboarding checklist", () => {
  beforeEach(() => window.localStorage.clear());

  it("counts sign-in as done and keeps each reader's ticks apart", () => {
    const { unmount } = render(<OnboardingChecklist principal="employee:p1" role="quality" />);
    expect(screen.getByTestId("onboarding-count")).toHaveTextContent("1 of 6 done");
    expect(screen.getByText(/a lot number or a product you release/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Ask one question"));
    expect(screen.getByTestId("onboarding-count")).toHaveTextContent("2 of 6 done");
    expect(JSON.parse(window.localStorage.getItem(checklistKey("employee:p1")) ?? "[]")).toEqual(["ask"]);
    unmount();

    render(<OnboardingChecklist principal="employee:p2" role="quality" />);
    expect(screen.getByTestId("onboarding-count")).toHaveTextContent("1 of 6 done");
  });

  it("keeps a reader's ticks after the page closes, and a tick can be undone", () => {
    window.localStorage.setItem(checklistKey("employee:p1"), JSON.stringify(["my_work", "feedback"]));
    render(<OnboardingChecklist principal="employee:p1" role={null} />);
    expect(screen.getByTestId("onboarding-count")).toHaveTextContent("3 of 6 done");
    fireEvent.click(screen.getByLabelText("Send one feedback note"));
    expect(screen.getByTestId("onboarding-count")).toHaveTextContent("2 of 6 done");
    expect(screen.getByLabelText("Sign in with Microsoft")).toBeDisabled();
  });
});

describe("a role's guide", () => {
  it("says what the Workbench does, how to ask, how opt-in works and where feedback goes", () => {
    render(<RoleGuide guide={guideFor("receivables")!} />);
    const guide = screen.getByTestId("role-guide");
    expect(guide).toHaveTextContent("Matches a customer remittance to the invoices it pays");
    expect(guide).toHaveTextContent("It only reads.");
    expect(screen.getByTestId("guide-asks")).toHaveTextContent("Which shipments from last week have no invoice yet?");
    expect(screen.getByTestId("guide-autopilot")).toHaveTextContent("Nobody else can turn it on for you");
    expect(screen.getByTestId("guide-autopilot")).toHaveTextContent("If its score drops below 90%, it turns itself off");
    expect(guide).toHaveTextContent("Remittance to apply");
    expect(screen.getByTestId("guide-feedback")).toBeInTheDocument();
  });
});
