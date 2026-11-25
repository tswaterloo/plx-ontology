"use client";

import { Download } from "lucide-react";
import { useState } from "react";
import { saveExcel } from "@/components/charts/export";
import { plainError } from "@/lib/errors";
import type { ExportXlsxRequest } from "@/lib/types";

/** One-click Excel (.xlsx) download of any chart, table or answer. */
export function ExcelExportButton({
  req,
  title,
  label = "Export to Excel",
  className = "inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer disabled:cursor-default disabled:opacity-50",
}: {
  req: ExportXlsxRequest | (() => ExportXlsxRequest);
  title?: string;
  label?: string;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const run = async () => {
    setBusy(true);
    setProblem("");
    try {
      const payload = typeof req === "function" ? req() : req;
      await saveExcel(payload, title);
    } catch (err) {
      setProblem(plainError(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="inline-flex items-center gap-x-2">
      <button type="button" className={className} disabled={busy} onClick={run} data-testid="excel-export-btn">
        <Download className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
        {busy ? "Exporting" : label}
      </button>
      {problem && <span className="text-[12px] text-late" role="alert">{problem}</span>}
    </span>
  );
}
