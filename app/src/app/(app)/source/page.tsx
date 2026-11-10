"use client";

import { ChevronLeft } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { AskBox } from "@/components/ask-box";
import { SourceLoader, sourceKind } from "@/components/source-view";
import { Toolbar } from "@/components/ui/toolbar";
import type { SourceView } from "@/lib/types";

function back() {
  if (window.history.length > 1) window.history.back();
  else window.location.assign("/");
}

function SourceReader() {
  const params = useSearchParams();
  const handle = params.get("handle") ?? "";
  const cited = params.getAll("field");
  const quote = params.get("quote");
  const [view, setView] = useState<SourceView | null>(null);
  const router = useRouter();
  // Ask about this document: the question goes with its citation (AskRequest.source); the
  // server reads it as this reader may, and answers about it.
  const askAbout = (question: string) =>
    router.push(`/ask?${new URLSearchParams({ q: question, source: handle }).toString()}`);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar
        title={
          <span className="inline-flex max-w-full items-center gap-1 align-middle text-[13px] font-normal tracking-normal">
            <button type="button" onClick={back} className="-ml-1.5 inline-flex h-7 shrink-0 items-center gap-0.5 rounded-md pr-1.5 pl-0.5 text-muted hover:bg-hover hover:text-fg cursor-pointer">
              <ChevronLeft className="size-4" strokeWidth={2} aria-hidden />
              Back
            </button>
            <span className="text-faint" aria-hidden>
              ›
            </span>
            <span className="truncate font-medium text-fg">{view ? sourceKind(view) : "Source"}</span>
          </span>
        }
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <SourceLoader key={handle} handle={handle} cited={cited} quote={quote} wide onView={setView} />
        {/* asking about a document is offered once it opened, never under "couldn't find that" */}
        {handle && view ? (
          <section aria-label="Ask about this" className="px-5 pb-12 sm:px-8 lg:px-12">
            <div className="max-w-[720px]">
              <AskBox size="md" onSubmit={(question) => askAbout(question)} placeholder="Ask about this document" />
            </div>
          </section>
        ) : null}
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
