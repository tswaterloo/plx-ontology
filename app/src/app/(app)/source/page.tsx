"use client";

import { ArrowLeft } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { SourceLoader } from "@/components/source-view";
import { Card, CardBody } from "@/components/ui/card";

function SourceReader() {
  const params = useSearchParams();
  const handle = params.get("handle") ?? "";
  const cited = params.getAll("field");
  const quote = params.get("quote");
  return (
    <div className="mx-auto w-full min-w-0 max-w-[760px] px-4 py-6 sm:px-6">
      <button
        type="button"
        onClick={() => (window.history.length > 1 ? window.history.back() : window.location.assign("/"))}
        className="mb-4 inline-flex min-h-9 items-center gap-1.5 rounded-lg px-2 py-1 text-row text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
      >
        <ArrowLeft className="size-4" aria-hidden /> Back
      </button>
      <Card>
        <CardBody className="min-w-0 pt-5 sm:pt-5">
          <SourceLoader key={handle} handle={handle} cited={cited} quote={quote} />
        </CardBody>
      </Card>
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
