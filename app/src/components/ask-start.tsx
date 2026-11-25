"use client";

import { ArrowRight } from "lucide-react";
import { useEffect, useState } from "react";
import { loadHome } from "@/components/shell";
import type { HomeResponse } from "@/lib/types";

type Starter = { text: string; objectId?: string };

const once = <T,>(xs: T[], key: (x: T) => string) => xs.filter((x, i) => xs.findIndex((y) => key(y) === key(x)) === i);

/** "Good morning, Ana": the hour the reader's own clock shows; the name is the one Home greets. */
export function greetingFor(name: string | undefined, now: Date = new Date()): string {
  const h = now.getHours();
  const part = h < 5 ? "Good evening" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  return name?.trim() ? `${part}, ${name.trim()}` : part;
}

/**
 * One-click starters from the reader's own work: today's list, the first late purchase order or
 * order at risk Home shows for them, and what they asked lately. Each is a real question about a
 * real record or their own history; nothing is a canned example.
 */
const RECORD_KINDS = new Set([
  "order",
  "sales_order",
  "purchase_order",
  "customer",
  "supplier",
  "batch",
  "product",
  "invoice",
  "shipment",
]);

/** Natural starter phrasing dispatched on typed AtRisk.kind (never word-guessing on display labels). */
export function riskStarter(risk: { label: string; kind?: string | null }): string {
  const text = risk.label.trim();
  const kind = (risk.kind ?? "").trim().toLowerCase();
  if (kind === "batch_qc_hold" || kind === "qc_hold") {
    return "Which open orders are affected by a batch on quality hold?";
  }
  if (!kind || RECORD_KINDS.has(kind)) {
    return `What is putting ${text} at risk?`;
  }
  const lower = text.charAt(0).toLowerCase() + text.slice(1);
  return `What needs attention for ${lower}?`;
}

export const DEFAULT_ASK_PROMPTS: readonly string[] = [
  "What do I need to do today?",
  "Which purchase orders are late?",
  "Show monthly revenue this year",
  "Which customers have overdue invoices?",
];

export function startersFrom(home: HomeResponse | null): Starter[] {
  const na = home?.needs_attention;
  const out: Starter[] = [{ text: DEFAULT_ASK_PROMPTS[0] }];
  const po = na?.late_pos?.[0];
  if (po) out.push({ text: `Why is ${po.label} late?`, objectId: po.object_id });
  const risk = na?.at_risk?.[0];
  if (risk) out.push({ text: riskStarter(risk), objectId: risk.object_id });
  const deduped = once(out, (s) => s.text.trim().toLowerCase());
  for (const text of DEFAULT_ASK_PROMPTS) {
    if (deduped.length >= 4) break;
    if (!deduped.some((s) => s.text.trim().toLowerCase() === text.toLowerCase())) {
      deduped.push({ text });
    }
  }
  return deduped;
}

/** The empty chat's quiet header, composer, and live operational starters. */
export function AskStart({ onAsk, subtitle, children }: { onAsk: (q: string, objectId?: string) => void; subtitle?: React.ReactNode; children: React.ReactNode }) {
  const [home, setHome] = useState<HomeResponse | null>(null);
  useEffect(() => {
    let live = true;
    loadHome()
      .then((h) => live && setHome(h))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  const starters = startersFrom(home);
  return (
    <div className="w-full">
      <div className="mb-5 flex flex-col items-center text-center">
        <h1 className="text-[22px] leading-7 font-semibold tracking-[-0.02em] text-fg" data-testid="ask-greeting">
          Ask Workbench
        </h1>
        {subtitle}
      </div>
      {children}
      <section aria-label="Ways to start" data-testid="ask-starters" className="mt-5">
        <ul className="divide-y divide-border border-y border-border">
          {starters.map((s) => (
            <li key={s.text}>
              <button
                type="button"
                onClick={() => onAsk(s.text, s.objectId)}
                className="group flex min-h-9 w-full items-center gap-3 px-1.5 py-2 text-left text-[13.5px] leading-5 text-muted transition-colors hover:bg-hover/60 hover:text-fg cursor-pointer"
              >
                <span className="min-w-0 flex-1 truncate">{s.text}</span>
                <ArrowRight className="size-3.5 shrink-0 text-faint opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" strokeWidth={2} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
