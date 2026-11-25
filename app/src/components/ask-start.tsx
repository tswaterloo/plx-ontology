"use client";

import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { loadHome } from "@/components/shell";
import { ago, formatDateTime } from "@/lib/format";
import type { HomeResponse, RecentQuestion } from "@/lib/types";

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

export function startersFrom(home: HomeResponse | null): Starter[] {
  const na = home?.needs_attention;
  const out: Starter[] = [{ text: "What do I need to do today?" }];
  const po = na?.late_pos?.[0];
  if (po) out.push({ text: `Why is ${po.label} late?`, objectId: po.object_id });
  const risk = na?.at_risk?.[0];
  if (risk) out.push({ text: riskStarter(risk), objectId: risk.object_id });
  const asked = once(
    [...(home?.recent_questions ?? [])].sort((a, b) => b.asked_at.localeCompare(a.asked_at)),
    (r) => r.question.trim().toLowerCase(),
  );
  for (const r of asked.slice(0, 2)) out.push({ text: r.question });
  return once(out, (s) => s.text.trim().toLowerCase());
}

/** The empty chat's greeting, starters and last chats. Nothing here is needed to ask: if Home can't be read only the greeting's plain form and the day's first starter remain. */
export function AskStart({ onAsk, children }: { onAsk: (q: string, objectId?: string) => void; children: React.ReactNode }) {
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
  const chats: RecentQuestion[] = once(
    [...(home?.recent_questions ?? [])].filter((r) => r.conversation_id).sort((a, b) => b.asked_at.localeCompare(a.asked_at)),
    (r) => r.conversation_id ?? "",
  ).slice(0, 3); // guard-ok: a preview of the 3 newest; every chat is in the Chats column beside it
  return (
    <>
      <h1 className="mb-5 text-center text-[28px] leading-9 font-semibold tracking-[-0.02em] max-sm:text-[24px]" data-testid="ask-greeting">
        {greetingFor(home?.greeting_name)}
      </h1>
      {children}
      <section aria-label="Ways to start" data-testid="ask-starters" className="mt-4">
        <ul>
          {starters.map((s) => (
            <li key={s.text}>
              <button
                type="button"
                onClick={() => onAsk(s.text, s.objectId)}
                className="group flex min-h-9 w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-[14px] leading-5 text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                <span className="min-w-0 flex-1 truncate">{s.text}</span>
                <ArrowUpRight className="size-3.5 shrink-0 opacity-0 group-hover:opacity-100 max-sm:opacity-60" strokeWidth={2} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      </section>
      {chats.length > 0 && (
        <section data-testid="ask-recent" aria-label="Recent chats" className="mt-6">
          <h2 className="mb-1 px-3 text-[12px] leading-4 font-medium text-muted">Recent</h2>
          <ul>
            {chats.map((r) => (
              <li key={r.conversation_id}>
                <Link
                  href={`/ask?c=${encodeURIComponent(r.conversation_id ?? "")}`}
                  title={`Asked ${formatDateTime(r.asked_at)}: open the answer`}
                  className="flex min-h-9 items-center gap-3 rounded-lg px-3 py-1.5 text-[14px] leading-5 hover:bg-hover"
                >
                  <span className="min-w-0 flex-1 truncate">{r.question}</span>
                  <span className="shrink-0 text-[12px] text-muted tabular-nums">{ago(r.asked_at)}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
