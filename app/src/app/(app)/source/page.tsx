"use client";

import { ArrowLeft } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { SourceLoader } from "@/components/source-view";

function SourceReader() {
  const params = useSearchParams();
  const handle = params.get("handle") ?? "";
  const cited = params.getAll("field");
  const quote = params.get("quote");
  return (
    <div className="mx-auto w-full min-w-0 max-w-[720px] px-4 pt-4 pb-16 sm:px-6">
      <button
        type="button"
        onClick={() => (window.history.length > 1 ? window.history.back() : window.location.assign("/"))}
        className="-ml-2 mb-4 inline-flex min-h-8 items-center gap-1.5 rounded px-2 py-1 text-meta text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
      >
        <ArrowLeft className="size-3.5" aria-hidden /> Back
      </button>
      <div className="min-w-0">
        <SourceLoader key={handle} handle={handle} cited={cited} quote={quote} />
      </div>
    </div>
  );
}

export default function SourcePage() {
  return (
    <Suspense fallback={null}>
      <SourceReader />
    </Suspense>
  );
}
