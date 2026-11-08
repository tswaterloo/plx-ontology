"use client";

// The tailored sections of a calendar event, meeting, task, approval, Teams thread or
// finance record (see lib/record-kinds.ts). Every section states what it shows or, plainly,
// why it is empty; rows come from the page's payload only.

import Link from "next/link";
import { kindSections } from "@/lib/record-kinds";
import type { ObjectPage } from "@/lib/types";
import { Property, SectionTitle } from "./parts";

export function KindSections({ obj }: { obj: ObjectPage }) {
  const sections = kindSections(obj);
  if (!sections) return null;
  return (
    <div className="space-y-8" data-testid="kind-sections">
      {sections.map((s) => (
        <section key={s.id} data-testid={`kind-${s.id}`}>
          <SectionTitle aside={s.rows.length > 0 ? <span className="text-[12px] text-muted">{s.rows.length}</span> : undefined}>{s.title}</SectionTitle>
          {s.rows.length === 0 ? (
            <p className="text-[13px] leading-5 text-muted" data-testid={`kind-${s.id}-empty`}>
              {s.empty}
            </p>
          ) : (
            <dl>
              {s.rows.map((r, i) => (
                <Property key={`${r.label}-${r.value}-${i}`} label={r.label}>
                  {r.href ? (
                    <Link href={r.href} className="hover:underline">
                      {r.value}
                    </Link>
                  ) : (
                    r.value
                  )}
                  {r.count ? <span className="ml-1.5 text-[12px] text-muted">×{r.count}</span> : null}
                </Property>
              ))}
            </dl>
          )}
        </section>
      ))}
    </div>
  );
}

