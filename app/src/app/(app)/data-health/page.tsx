"use client";

import { SectionTabs } from "@/components/shell";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { DataHealthSection } from "@/components/watching/alert-kinds";

/** Admin, Data health: records to fix (duplicate parties, missing fields, sync mismatches),
 * inside Admin with Admin's tabs (it opened Home's Alerts tab before, and Admin's tabs went). */
export default function DataHealthPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Data health" />
      <SectionTabs section="admin" />
      <PageIntro path="/data-health" />
      <div className="w-full max-w-4xl overflow-y-auto px-6 py-5" data-testid="data-health-page">
        <DataHealthSection titled={false} />
      </div>
    </div>
  );
}
