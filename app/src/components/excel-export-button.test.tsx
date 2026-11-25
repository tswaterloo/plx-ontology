import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const saveExcel = vi.fn();
vi.mock("@/components/charts/export", () => ({
  saveExcel: (...a: unknown[]) => saveExcel(...a),
}));

import { ExcelExportButton } from "./excel-export-button";

describe("ExcelExportButton", () => {
  it("exports a table or answer payload as an Excel workbook on click", async () => {
    saveExcel.mockResolvedValue(undefined);
    const payload = {
      title: "Open POs",
      tables: [{ title: "Open POs", columns: ["PO", "Total"], rows: [["PO1001", "12500.00"]], sql: "SELECT 1" }],
    };
    render(<ExcelExportButton req={payload} title="Open POs" />);
    fireEvent.click(screen.getByTestId("excel-export-btn"));
    await waitFor(() => expect(saveExcel).toHaveBeenCalledWith(payload, "Open POs"));
  });
});
