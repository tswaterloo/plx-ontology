"use client";

import { ArrowRight, BarChart3, ClipboardList, Factory, MessageSquare, ShieldCheck, ShoppingCart, Terminal, TrendingUp, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ASK_SKILLS, type AskSkill } from "@/components/ask-box";
import { cleanChatTitle, compactAge } from "@/components/ask-chats";
import { loadHome } from "@/components/shell";
import { getAskChats } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { AskChat, HomeResponse } from "@/lib/types";

type Starter = { text: string; objectId?: string; tag?: string };

export type AskSpaceId = "all" | "finance" | "purchasing" | "operations" | "quality" | "sales";

export interface AskSpace {
  id: AskSpaceId;
  label: string;
  hint: string;
  /** Clean domain focus prefix attached on the wire when asking inside a scoped Genie Space. */
  promptPrefix?: string;
  starters: readonly string[];
}

export const ASK_SPACES: readonly AskSpace[] = [
  {
    id: "all",
    label: "All Petra",
    hint: "Orders, suppliers, lots, finance, and mail across Petra",
    starters: [
      "What do I need to do today?",
      "Which purchase orders are late?",
      "Show monthly revenue this year",
      "Which customers have overdue invoices?",
    ],
  },
  {
    id: "finance",
    label: "Finance",
    hint: "General ledger, margins, AR aging, and AP payables",
    promptPrefix: "Focus on Finance (general ledger, margins, AR aging, and AP payables):",
    starters: [
      "Show monthly revenue and gross margin this year",
      "Which customers have overdue AR invoices?",
      "What do we owe our top suppliers in AP right now?",
      "Which shipped orders have not been invoiced yet?",
    ],
  },
  {
    id: "purchasing",
    label: "Purchasing",
    hint: "Late POs, supplier confirmations, and lead times",
    promptPrefix: "Focus on Purchasing (purchase orders, supplier confirmations, and lead times):",
    starters: [
      "Which purchase orders are late with no supplier confirmation?",
      "Show average supplier lead times this quarter",
      "Which open purchase orders have the largest past-due value?",
      "Which suppliers had the most late deliveries this year?",
    ],
  },
  {
    id: "operations",
    label: "Operations",
    hint: "Shipments, assembly orders, production, and inventory",
    promptPrefix: "Focus on Operations (shipments, assembly orders, production, and inventory):",
    starters: [
      "Which assembly orders are waiting on materials?",
      "Show open customer shipments scheduled for this week",
      "Which items are low in inventory or below reorder point?",
      "Which production batches are scheduled to finish next?",
    ],
  },
  {
    id: "quality",
    label: "Quality",
    hint: "Lot releases, COAs, quality holds, and batch traceability",
    promptPrefix: "Focus on Quality (lots, batches, COAs, and quality holds):",
    starters: [
      "Which batches or lots are currently on quality hold?",
      "Which open shipments are waiting on a COA?",
      "Show lot traceability for active customer orders",
      "Which incoming raw material lots need QC inspection?",
    ],
  },
  {
    id: "sales",
    label: "Sales",
    hint: "Customer orders, pricing, revenue, and open demand",
    promptPrefix: "Focus on Sales (customer orders, pricing, revenue, and open demand):",
    starters: [
      "Show top 10 customers by invoiced sales this year",
      "Which open sales orders are at risk of missing ship date?",
      "Which customer items are priced below cost or low margin?",
      "Summarize open customer demand and backorders by product",
    ],
  },
];

export function spaceById(id: AskSpaceId | string | null | undefined): AskSpace {
  return ASK_SPACES.find((s) => s.id === id) ?? ASK_SPACES[0];
}

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

export const DEFAULT_ASK_PROMPTS: readonly string[] = ASK_SPACES[0].starters;

const DEFAULT_PROMPT_TAGS: readonly string[] = ["Today", "Purchasing", "Finance", "Finance"];

const TAG_ICON: Record<string, LucideIcon> = {
  Today: ClipboardList,
  Finance: BarChart3,
  Purchasing: ShoppingCart,
  Operations: Factory,
  Quality: ShieldCheck,
  Sales: TrendingUp,
};

export function startersFrom(home: HomeResponse | null, spaceId: AskSpaceId = "all"): Starter[] {
  const space = spaceById(spaceId);
  if (space.id !== "all") {
    return space.starters.map((text) => ({ text, tag: space.label }));
  }
  const na = home?.needs_attention;
  const out: Starter[] = [{ text: DEFAULT_ASK_PROMPTS[0], tag: DEFAULT_PROMPT_TAGS[0] }];
  const po = na?.late_pos?.[0];
  if (po) out.push({ text: `Why is ${po.label} late?`, objectId: po.object_id, tag: "Purchasing" });
  const risk = na?.at_risk?.[0];
  if (risk) out.push({ text: riskStarter(risk), objectId: risk.object_id, tag: "Operations" });
  const deduped = once(out, (s) => s.text.trim().toLowerCase());
  for (let i = 0; i < DEFAULT_ASK_PROMPTS.length; i++) {
    const text = DEFAULT_ASK_PROMPTS[i];
    if (deduped.length >= 4) break;
    if (!deduped.some((s) => s.text.trim().toLowerCase() === text.toLowerCase())) {
      deduped.push({ text, tag: DEFAULT_PROMPT_TAGS[i] ?? "All Petra" });
    }
  }
  return deduped;
}

/** Apple-grade Liquid Glass segmented switcher for Genie Spaces (department-scoped Ask agents). */
export function SpaceSwitcher({
  value,
  onChange,
  compact = false,
  className,
}: {
  value: AskSpaceId;
  onChange: (id: AskSpaceId) => void;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      aria-label="Genie spaces"
      data-testid="genie-spaces"
      className={cn(
        "liquid-glass-pill inline-flex max-w-full flex-wrap items-center gap-0.5",
        compact ? "rounded-[11px] p-0.5" : "rounded-[14px] p-1",
        className,
      )}
    >
      {ASK_SPACES.map((space) => {
        const active = space.id === value;
        return (
          <button
            key={space.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(space.id)}
            data-testid={`space-pill-${space.id}`}
            className={cn(
              "inline-flex items-center border border-transparent font-medium transition-all duration-180 active:scale-[0.98] cursor-pointer",
              compact ? "h-6.5 rounded-[8px] px-2.5 text-[11.5px]" : "h-8 rounded-[10px] px-3.5 text-[12.5px]",
              active
                ? "liquid-glass-active font-semibold"
                : "text-muted hover:bg-hover hover:text-fg",
            )}
          >
            {space.label}
          </button>
        );
      })}
    </div>
  );
}

type QuickMatch =
  | { kind: "chat"; id: string; title: string; age: string; href: string }
  | { kind: "skill"; skill: AskSkill };

/** The empty chat's centered header, Apple Liquid Glass scope bar, composer, live quick-jump strip, and clean starter prompt rows. */
export function AskStart({
  onAsk,
  spaceId = "all",
  onSpaceChange,
  draftQuery = "",
  subtitle,
  children,
}: {
  onAsk: (q: string, objectId?: string) => void;
  spaceId?: AskSpaceId;
  onSpaceChange?: (id: AskSpaceId) => void;
  /** Live composer text used to surface instant matching chats and workflow skills on the start screen. */
  draftQuery?: string;
  subtitle?: React.ReactNode;
  children: React.ReactNode;
}) {
  const [home, setHome] = useState<HomeResponse | null>(null);
  const [recentChats, setRecentChats] = useState<AskChat[]>([]);
  useEffect(() => {
    let live = true;
    loadHome()
      .then((h) => live && setHome(h))
      .catch(() => undefined);
    getAskChats("")
      .then((res) => {
        if (live && res.available) setRecentChats(res.chats.slice(0, 30));
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  const quickMatches = useMemo<QuickMatch[]>(() => {
    const q = draftQuery.trim().toLowerCase();
    if (q.length < 2 || q.startsWith("@")) return [];
    const normalized = q.startsWith("/") ? q.slice(1) : q;
    const out: QuickMatch[] = [];
    const matchedSkills = ASK_SKILLS.filter(
      (s) =>
        s.command.toLowerCase().includes(q) ||
        (normalized.length > 0 && (s.label.toLowerCase().includes(normalized) || s.description.toLowerCase().includes(normalized))),
    ).slice(0, 3);
    for (const skill of matchedSkills) out.push({ kind: "skill", skill });

    if (!q.startsWith("/")) {
      const seenTitles = new Set<string>();
      for (const c of recentChats) {
        const title = cleanChatTitle(c.title, c.named);
        const key = title.toLowerCase();
        if (seenTitles.has(key) || !key.includes(q)) continue;
        seenTitles.add(key);
        out.push({
          kind: "chat",
          id: c.conversation_id,
          title,
          age: compactAge(c.last_at),
          href: `/ask?c=${encodeURIComponent(c.conversation_id)}`,
        });
        if (out.length >= 5) break;
      }
    }
    return out.slice(0, 5);
  }, [draftQuery, recentChats]);

  const space = spaceById(spaceId);
  const starters = startersFrom(home, spaceId);
  return (
    <div className="w-full">
      <div className="mb-7 flex flex-col items-center text-center">
        <h1
          className="font-display text-[32px] leading-[38px] font-semibold tracking-[-0.03em] text-fg sm:text-[38px] sm:leading-[44px]"
          data-testid="ask-greeting"
        >
          {space.id === "all" ? "Ask Workbench" : `Ask ${space.label}`}
        </h1>
        {onSpaceChange && (
          <div className="mt-5 flex flex-col items-center gap-2.5">
            <SpaceSwitcher value={spaceId} onChange={onSpaceChange} />
            <p className="text-[12.5px] leading-4 text-muted" data-testid="space-scope-hint">
              {space.hint}
            </p>
          </div>
        )}
        {!onSpaceChange && subtitle}
      </div>
      {children}
      {quickMatches.length > 0 && (
        <section
          aria-label="Quick matches"
          data-testid="ask-quick-jump"
          className="mt-3 rounded-xl border border-border bg-surface/85 py-1.5 plx-panel-animate"
        >
          <p className="px-3 pb-1 text-[11px] font-semibold tracking-[0.02em] text-faint">
            Matching skills and recent chats
          </p>
          <ul className="divide-y divide-border/50">
            {quickMatches.map((m) =>
              m.kind === "skill" ? (
                <li key={`skill-${m.skill.id}`}>
                  <button
                    type="button"
                    onClick={() => onAsk(m.skill.prompt)}
                    className="group flex w-full items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors hover:bg-hover cursor-pointer"
                  >
                    <Terminal className="size-3.5 shrink-0 text-accent" strokeWidth={2} aria-hidden />
                    <span className="shrink-0 font-mono text-[12px] font-medium text-accent">{m.skill.command}</span>
                    <span className="min-w-0 flex-1 truncate text-muted group-hover:text-fg">{m.skill.description}</span>
                    <ArrowRight className="size-3.5 shrink-0 text-faint opacity-0 transition-opacity group-hover:opacity-100" strokeWidth={2} aria-hidden />
                  </button>
                </li>
              ) : (
                <li key={`chat-${m.id}`}>
                  <Link
                    href={m.href}
                    className="group flex w-full items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors hover:bg-hover"
                  >
                    <MessageSquare className="size-3.5 shrink-0 text-muted" strokeWidth={2} aria-hidden />
                    <span className="min-w-0 flex-1 truncate font-medium text-fg">{m.title}</span>
                    {m.age && <span className="shrink-0 text-[11px] text-faint tabular-nums">{m.age}</span>}
                  </Link>
                </li>
              ),
            )}
          </ul>
        </section>
      )}
      <section aria-label="Ways to start" data-testid="ask-starters" className="mt-7">
        <ul className="divide-y divide-border/70 border-y border-border/70">
          {starters.map((s) => {
            const tag = s.tag ?? space.label;
            const Icon = TAG_ICON[tag] ?? ClipboardList;
            return (
              <li
                key={s.text}
                data-testid="ask-starter-card"
                className="group relative flex items-center gap-3 px-2 py-3 text-left transition-colors duration-150 hover:bg-hover focus-within:bg-hover"
              >
                <Icon
                  className="size-4 shrink-0 text-faint transition-colors duration-150 group-hover:text-accent"
                  strokeWidth={1.85}
                  aria-hidden
                />
                <button
                  type="button"
                  onClick={() => onAsk(s.text, s.objectId)}
                  className="min-w-0 flex-1 text-left text-[13.5px] leading-5 font-medium text-muted transition-colors duration-150 group-hover:text-fg focus-visible:outline-none after:absolute after:inset-0 cursor-pointer"
                >
                  {s.text}
                </button>
                <span
                  className="shrink-0 text-[11.5px] font-medium text-faint transition-colors group-hover:text-muted"
                  data-testid="starter-tag"
                >
                  {tag}
                </span>
                <ArrowRight
                  className="size-3.5 shrink-0 -translate-x-0.5 text-faint opacity-0 transition-all duration-150 group-hover:translate-x-0 group-hover:text-fg group-hover:opacity-100 group-focus-within:translate-x-0 group-focus-within:opacity-100"
                  strokeWidth={2}
                  aria-hidden
                />
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
