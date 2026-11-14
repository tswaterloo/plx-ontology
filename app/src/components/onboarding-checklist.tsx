"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { checklistFor, readDone, writeDone, type GuideRole } from "@/lib/onboarding";

/**
 * The week-one checklist: six steps a new reader ticks off as they go. The ticks are kept per
 * reader in this browser only; nothing is sent to the server. Signing in counts as done
 * because the reader is reading this.
 */
export function OnboardingChecklist({ principal, role }: { principal: string; role: GuideRole | null }) {
  const steps = checklistFor(role);
  const [done, setDone] = useState<string[]>([]);
  useEffect(() => setDone(readDone(principal)), [principal]);
  const isDone = (id: string) => id === "sign_in" || done.includes(id);
  const count = steps.filter((s) => isDone(s.id)).length;
  const toggle = (id: string) => {
    const next = done.includes(id) ? done.filter((d) => d !== id) : [...done, id];
    setDone(next);
    writeDone(principal, next);
  };
  return (
    <section aria-labelledby="onboarding-checklist" className="space-y-2" data-testid="onboarding-checklist">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="onboarding-checklist" className="text-[13px] font-semibold">
          Your first week
        </h2>
        <p className="num text-[12px] text-muted" data-testid="onboarding-count">
          {count} of {steps.length} done
        </p>
      </div>
      <ol className="space-y-1.5">
        {steps.map((s) => (
          <li key={s.id} className="flex items-start gap-2.5" data-testid="onboarding-step">
            <input
              id={`step-${s.id}`}
              type="checkbox"
              className="mt-0.5 size-3.5 cursor-pointer accent-[var(--fg)]"
              checked={isDone(s.id)}
              disabled={s.id === "sign_in"}
              onChange={() => toggle(s.id)}
            />
            <div className="min-w-0 text-[12px]">
              <label htmlFor={`step-${s.id}`} className="cursor-pointer text-[13px] font-medium">
                {s.label}
              </label>
              <p className="text-muted">
                {s.hint}
                {s.href && (
                  <>
                    {" "}
                    <Link href={s.href} className="underline">
                      Open
                    </Link>
                  </>
                )}
              </p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
