"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { SourceLoader } from "@/components/source-view";

function SourceReader() {
  const params = useSearchParams();
  const handle = params.get("handle") ?? "";
  const cited = params.getAll("field");
  const quote = params.get("quote");
  return (
    <div className="mx-auto w-full min-w-0 max-w-[1200px] px-4 pt-4 pb-16 sm:px-8">
      <div className="max-w-[760px]">
      <button
        type="button"
        onClick={() => (window.history.length > 1 ? window.history.back() : window.location.assign("/"))}
        className="mb-3 text-meta text-accent hover:underline cursor-pointer"
      >
        Back
      </button>
      <div className="min-w-0">
        <SourceLoader key={handle} handle={handle} cited={cited} quote={quote} />
      </div>
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
