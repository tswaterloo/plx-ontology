import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Template, TemplateFilled } from "@/lib/types";
import { ApiError } from "@/lib/errors";
import { SaveAsTemplate, sharesOf, TemplateFilledView, UseTemplate } from "./templates";

const api = vi.hoisted(() => ({
  proposeTemplate: vi.fn(),
  getTemplates: vi.fn(),
  saveTemplate: vi.fn(),
  suggestTemplates: vi.fn(),
  fillTemplate: vi.fn(),
}));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));

const filled: TemplateFilled = {
  template_id: 7,
  name: "Order confirmation",
  doc_type: "order_confirmation",
  text: "Dear Acme Foods,\nYour PO PO-7781 ships [check: ship date].",
  style_notes: "",
  record: { object_id: "SalesOrder:1", type: "SalesOrder", label: "SO-5512", open_url: "/object/SalesOrder/1" },
  fields: [
    { name: "customer_name", label: "customer name", type: "text", status: "filled", value: "Acme Foods", reason: "", citation: { record: "Customer:9", field: "name", source: "api://c/9", open_url: "/source?handle=api%3A%2F%2Fc%2F9" }, also: [] },
    { name: "ship_date", label: "ship date", type: "date", status: "check", value: "", reason: "no record read holds it", citation: null, also: [] },
  ],
  checks: 1,
  notes: [],
};

const template = (over: Partial<Template> = {}): Template => ({
  id: 7, name: "Order confirmation", doc_type: "order_confirmation", body: "Dear {{customer_name}}", placeholders: [],
  style_notes: "", shared_with: [], mine: true, uses: 2, created_at: "2026-11-13T00:00:00Z", updated_at: "2026-11-13T00:00:00Z", ...over,
});

describe("a filled template", () => {
  it("shows each value with its source and every unsettled one as check with why", () => {
    render(<TemplateFilledView filled={filled} />);
    expect(screen.getByTestId("template-text").textContent).toContain("[check: ship date]");
    const rows = screen.getByTestId("template-fields").querySelectorAll("tbody tr");
    expect(rows[0].getAttribute("data-status")).toBe("filled");
    expect(rows[0].querySelector("a")?.getAttribute("href")).toBe("/source?handle=api%3A%2F%2Fc%2F9");
    expect(rows[1].textContent).toContain("Check");
    expect(rows[1].textContent).toContain("no record read holds it");
  });
});

describe("sharing", () => {
  it("reads addresses as people and other words as roles; empty keeps it private", () => {
    expect(sharesOf("Ben@plx.example, purchasing; customer service")).toEqual(["person:ben@plx.example", "role:purchasing", "role:customer_service"]);
    expect(sharesOf("  ")).toEqual([]);
  });
});

describe("save as template", () => {
  it("proposes placeholders from the record, then saves what the person checked", async () => {
    api.proposeTemplate.mockResolvedValue({ body: "Hi {{customer_name}}", replaced: [{ name: "customer_name", value: "Acme Foods" }], record: null });
    api.getTemplates.mockResolvedValue({ available: true, templates: [], kinds: [], fields: [], share_roles: ["purchasing"] });
    api.saveTemplate.mockResolvedValue(template({ name: "Hello" }));
    render(<SaveAsTemplate text="Hi Acme Foods" record="SalesOrder:1" docType="customer_email" />);
    fireEvent.click(screen.getByTestId("save-as-template"));
    await waitFor(() => expect((screen.getByTestId("template-body") as HTMLTextAreaElement).value).toBe("Hi {{customer_name}}"));
    expect(api.proposeTemplate).toHaveBeenCalledWith("Hi Acme Foods", "SalesOrder:1");
    fireEvent.change(screen.getByTestId("template-name"), { target: { value: "Hello" } });
    fireEvent.change(screen.getByTestId("template-share"), { target: { value: "ben@plx.example" } });
    fireEvent.click(screen.getByTestId("template-save"));
    await waitFor(() => expect(screen.getByTestId("template-saved")).toBeTruthy());
    expect(api.saveTemplate.mock.calls[0][0]).toMatchObject({ name: "Hello", doc_type: "customer_email", body: "Hi {{customer_name}}", shared_with: ["person:ben@plx.example"] });
  });

  it("shows the server's refusal (pay or finance) and keeps the form open", async () => {
    api.proposeTemplate.mockResolvedValue({ body: "Price is $40.00", replaced: [], record: null });
    api.getTemplates.mockResolvedValue({ available: true, templates: [], kinds: [], fields: [], share_roles: [] });
    api.saveTemplate.mockRejectedValue(new ApiError(422, "template_refused", undefined, "Templates never keep pay or finance."));
    render(<SaveAsTemplate text="Price is $40.00" docType="quote" />);
    fireEvent.click(screen.getByTestId("save-as-template"));
    await waitFor(() => screen.getByTestId("template-name"));
    fireEvent.change(screen.getByTestId("template-name"), { target: { value: "Q" } });
    fireEvent.click(screen.getByTestId("template-save"));
    await waitFor(() => expect(screen.getByTestId("template-problem").textContent).toContain("pay or finance"));
    expect(screen.queryByTestId("template-saved")).toBeNull();
  });
});

describe("use template", () => {
  it("offers the person's templates for the document and fills one from the record", async () => {
    api.suggestTemplates.mockResolvedValue({ doc_type: "order_confirmation", suggestions: [{ kind: "saved", doc_type: "order_confirmation", label: "Order confirmation", detail: "yours, used 2 times", template: template() }] });
    api.fillTemplate.mockResolvedValue(filled);
    render(<UseTemplate docTypes={["order_confirmation"]} record="SalesOrder:1" />);
    fireEvent.click(await screen.findByTestId("template-offer"));
    await waitFor(() => screen.getByTestId("template-filled"));
    expect(api.fillTemplate).toHaveBeenCalledWith(7, "SalesOrder:1");
  });

  it("shows nothing when there is no template for it", async () => {
    api.suggestTemplates.mockResolvedValue({ doc_type: "quote", suggestions: [] });
    const { container } = render(<UseTemplate docTypes={["quote"]} />);
    await waitFor(() => expect(api.suggestTemplates).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });
});
