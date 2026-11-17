"use client";

import { Download } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { BriefView } from "@/components/brief-view";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { SectionTabs } from "@/components/shell";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { getQcAudit, getQcAuditText, getQcDaily } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { useRead } from "@/lib/use-read";
import type { Brief } from "@/lib/types";

const VIEWS = [
  { key: "daily", label: "Daily pack", read: getQcDaily, waiting: "Reading today's QC pack" },
  { key: "audit", label: "Audit pack", read: getQcAudit, waiting: "Reading the QC audit pack" },
] as const;

/** Every section held back for quality control: this reader holds no quality grant. */
function heldBack(brief: Brief): boolean {
  return brief.sections.length > 0 && brief.sections.every((s) => s.state === "quality_marked");
}

/** The audit pack's plain-text page, saved as a file (the read carries the reader's sign-in). */
function useAuditDownload() {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const text = await getQcAuditText();
      const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `qc-audit-${new Date().toISOString().slice(0, 10)}.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, problem, run };
}

function Qc() {
  const params = useSearchParams();
  const router = useRouter();
  const view = VIEWS.find((v) => v.key === params.get("view")) ?? VIEWS[0];
  const { data: brief, error, retry } = useRead<Brief>(`qc ${view.key}`, (signal) => view.read(signal));
  const download = useAuditDownload();
  const blocked = !!brief && heldBack(brief);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar
        heading
        title="Quality"
        pin={`Quality: ${view.label}`}
        actions={
          view.key === "audit" && brief && !blocked ? (
            <ToolbarButton onClick={download.run} disabled={download.busy} data-testid="qc-audit-download">
              <Download aria-hidden />
              {download.busy ? "Preparing" : "Download as text"}
            </ToolbarButton>
          ) : undefined
        }
      />
      <SectionTabs section="watching" />
      <div className="w-full max-w-[960px] overflow-y-auto p-5">
        <div role="tablist" aria-label="QC pack" className="mb-4 flex h-9 gap-5 border-b border-border">
          {VIEWS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={v.key === view.key}
              onClick={() => router.replace(v.key === "daily" ? "/qc" : `/qc?view=${v.key}`)}
              className={cn("-mb-px border-b-2 text-[13px] cursor-pointer", v.key === view.key ? "border-accent font-medium text-fg" : "border-transparent text-muted hover:text-fg")}
            >
              {v.label}
            </button>
          ))}
        </div>
        {download.problem && <p className="mb-3 text-[13px] text-[color:var(--risk)]" role="alert">The audit pack couldn&rsquo;t be downloaded: {download.problem}</p>}
        <div role="tabpanel" aria-label={view.label}>
          {error ? (
            <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
          ) : !brief ? (
            <Pending page label={view.waiting} />
          ) : blocked ? (
            <div className="text-[13px]" data-testid="qc-held-back">
              <p>The QC packs are for readers who hold quality control.</p>
              <p className="mt-0.5 text-muted">Ask the owner if you need it; nothing in them is shown to you until then.</p>
            </div>
          ) : (
            <BriefView brief={brief} />
          )}
        </div>
      </div>
    </div>
  );
}

/** Quality: the QC daily pack (open checks, holds and the hold-release queue) and the QC audit pack. */
export default function QcPage() {
  return (
    <Suspense>
      <Qc />
    </Suspense>
  );
}
