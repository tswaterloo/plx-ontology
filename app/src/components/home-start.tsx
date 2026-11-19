"use client";

import { Pin, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AskBox } from "./ask-box";
import { useAuth } from "./auth";
import { stashPending } from "@/lib/pending";
import { togglePin, usePins } from "@/lib/pins";
import { useSaved } from "@/lib/saved";
import { cn } from "@/lib/cn";

/** Home's Ask box, on top of the list: the question opens Ask. */
export function HomeAsk({ className }: { className?: string }) {
  const router = useRouter();
  return (
    <div className={cn("w-full", className)} data-testid="home-ask">
      <AskBox
        size="md"
        label="Ask or tell the Workbench"
        placeholder="Ask about an order, supplier, customer or lot"
        onSubmit={(question, chosen) => {
          stashPending(question, chosen);
          router.push(`/ask?q=${encodeURIComponent(question)}`);
        }}
      />
    </div>
  );
}

/**
 * My work's second part, after what needs you: the Ask box, then the person's pinned pages
 * (their role's defaults until they pin or unpin anything; any page's pin button adds one).
 */
export function HomeStart({ className }: { className?: string }) {
  const router = useRouter();
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const items = useSaved();
  const { pins, defaults } = usePins(me);
  return (
    <div className={cn("w-full max-w-[560px]", className)} data-testid="home-start">
      <h2 className="text-[13px] font-semibold">Ask or tell</h2>
      <div className="mt-2">
        <AskBox
          size="md"
          label="Ask or tell the Workbench"
          placeholder="Ask about an order, supplier, customer or lot"
          onSubmit={(question, chosen) => {
            stashPending(question, chosen);
            router.push(`/ask?q=${encodeURIComponent(question)}`);
          }}
        />
      </div>
      <h2 className="mt-7 text-[13px] font-semibold">{defaults ? "Pinned for your role" : "Pinned"}</h2>
      {defaults && <p className="mt-0.5 text-[12px] text-muted">Your role's usual pages. Pin any page with the pin at its top right and this becomes your own list.</p>}
      {pins.length === 0 ? (
        <p className="mt-1 text-[13px] text-muted" data-testid="pins-empty">Nothing pinned. Pin a page with the pin at the top right of it.</p>
      ) : (
        <ul className="mt-2 divide-y divide-border border-y border-border" data-testid="home-pins">
          {pins.map((p) => (
            <li key={p.href} className="group flex h-9 items-center gap-2.5">
              <Pin className="size-3.5 shrink-0 text-faint" aria-hidden />
              <Link href={p.href} className="min-w-0 flex-1 truncate text-[13px] hover:underline">
                {p.label}
              </Link>
              <button
                type="button"
                onClick={() => togglePin(p, items, me)}
                aria-label={`Unpin ${p.label}`}
                title="Unpin"
                className="flex size-7 items-center justify-center rounded text-faint opacity-0 hover:bg-hover hover:text-fg focus-visible:opacity-100 group-hover:opacity-100 cursor-pointer max-lg:opacity-100"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
