"use client";

import { ChevronDown, History, SlidersHorizontal, Sparkles } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { AssistWorkflowsView } from "@/components/autopilot/assist-workflows";
import { AuditView } from "@/components/autopilot/audit";
import { AutopilotView } from "@/components/autopilot/area";
import { AssistMemoryView } from "@/components/autopilot/memory-tab";
import { AssistSuggestionsView } from "@/components/autopilot/suggestions-tab";
import { AssistWorkbench } from "@/components/autopilot/workbench";
import { FeedNote } from "@/components/feed-note";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { StoredAge } from "@/components/ui/stored-age";
import { getAutopilot, getRecurring, getTeamsConnection } from "@/lib/api";
import { NO_FILTERS } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { useStaleRefresh } from "@/lib/stale-refresh";
import type { AutopilotArea } from "@/lib/types";
import { useRead } from "@/lib/use-read";

// Assist studio: 3 primary workspaces (Inbox, Suggestions, Workflows) plus a compact
// Memory & Rules popover for persistent memory, activity log, and switches.
// Supports both ?show= and ?tab= query parameters for deep links.
type Show = "prepared" | "suggestions" | "workflows" | "memory" | "log" | "switches";

function resolveShow(raw: string | null): Show {
  if (!raw) return "prepared";
  const key = raw.toLowerCase();
  if (key === "inbox" || key === "queue" || key === "prepared") return "prepared";
  if (key === "suggestions") return "suggestions";
  if (key === "workflows" || key === "routines") return "workflows";
  if (key === "memory") return "memory";
  if (key === "history" || key === "log") return "log";
  if (key === "switches") return "switches";
  return "prepared";
}

const PRIMARY_TABS: { id: "prepared" | "suggestions" | "workflows"; label: string; href: string }[] = [
  { id: "prepared", label: "Inbox", href: "/assist" },
  { id: "suggestions", label: "Suggestions", href: "/assist?show=suggestions" },
  { id: "workflows", label: "Workflows", href: "/assist?show=workflows" },
];

const SECONDARY_ITEMS: {
  id: "memory" | "log" | "switches";
  label: string;
  description: string;
  href: string;
  icon: typeof Sparkles;
}[] = [
  {
    id: "memory",
    label: "Memory",
    description: "Saved preferences and standing rules",
    href: "/assist?show=memory",
    icon: Sparkles,
  },
  {
    id: "log",
    label: "Activity log",
    description: "What Assist prepared and sent",
    href: "/assist?show=log",
    icon: History,
  },
  {
    id: "switches",
    label: "Switches",
    description: "Autopilot controls and connections",
    href: "/assist?show=switches",
    icon: SlidersHorizontal,
  },
];

export default function AssistPage() {
  return (
    <Suspense>
      <Assist />
    </Suspense>
  );
}

function MemoryRulesMenu({ active }: { active: Show }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const activeSecondary = SECONDARY_ITEMS.find((item) => item.id === active) ?? null;

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={boxRef} className="relative shrink-0">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid="assist-memory-rules-trigger"
        className={cn(
          "inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-[12.5px] font-medium transition-colors cursor-pointer",
          activeSecondary || open
            ? "border-border-strong bg-surface text-fg"
            : "border-border bg-bg text-muted hover:border-border-strong hover:text-fg",
        )}
      >
        <SlidersHorizontal className="size-3.5 shrink-0 text-muted" aria-hidden />
        <span>{activeSecondary ? `Memory & Rules · ${activeSecondary.label}` : "Memory & Rules"}</span>
        <ChevronDown
          className={cn("size-3.5 shrink-0 text-faint transition-transform duration-150", open && "rotate-180")}
          aria-hidden
        />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Memory and rules"
          data-testid="assist-memory-rules-menu"
          className="absolute right-0 top-full z-40 mt-1.5 w-64 rounded-xl border border-border-strong bg-raised p-1.5 shadow-pop"
        >
          {SECONDARY_ITEMS.map((item) => {
            const Icon = item.icon;
            const isCurrent = active === item.id;
            return (
              <Link
                key={item.id}
                href={item.href}
                role="menuitem"
                aria-current={isCurrent ? "page" : undefined}
                onClick={() => setOpen(false)}
                className={cn(
                  "flex items-start gap-2.5 rounded-lg px-2.5 py-2 transition-colors",
                  isCurrent ? "bg-row-on text-fg" : "text-fg hover:bg-hover",
                )}
              >
                <Icon className="mt-0.5 size-3.5 shrink-0 text-muted" aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium leading-tight text-fg">{item.label}</span>
                  <span className="mt-0.5 block text-[11.5px] leading-snug text-muted">{item.description}</span>
                </span>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Assist() {
  const params = useSearchParams();
  const show = resolveShow(params.get("show") ?? params.get("tab"));
  const isSplitPane = show === "prepared" || show === "suggestions";

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg" data-own-scroll={isSplitPane ? "" : undefined}>
      {/* Clean single-row h-12 studio header: Liquid Glass 3-tab switcher left, Memory & Rules right */}
      <header
        className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border bg-bg px-4 lg:px-5"
        data-testid="assist-studio-header"
      >
        <div className="flex min-w-0 items-center gap-3">
          <h1 className="shrink-0 text-[15px] font-semibold tracking-[-0.02em] text-fg">Assist</h1>
          <nav
            aria-label="Assist views"
            data-testid="assist-primary-tabs"
            className="liquid-glass-pill inline-flex items-center gap-0.5 rounded-lg p-0.5"
          >
            {PRIMARY_TABS.map((tab) => {
              const isActive = show === tab.id;
              return (
                <Link
                  key={tab.id}
                  href={tab.href}
                  aria-current={isActive ? "page" : undefined}
                  data-testid={`assist-tab-${tab.id}`}
                  className={cn(
                    "inline-flex h-7 items-center rounded-md px-3 text-[12.5px] font-medium transition-colors whitespace-nowrap",
                    isActive ? "liquid-glass-active" : "text-muted hover:text-fg",
                  )}
                >
                  {tab.label}
                </Link>
              );
            })}
          </nav>
        </div>

        <MemoryRulesMenu active={show} />
      </header>
      <PageIntro path="/assist" />

      <div
        className={
          isSplitPane
            ? "flex min-h-0 w-full flex-1 flex-col overflow-hidden"
            : "flex min-h-0 w-full flex-1 flex-col overflow-y-auto px-4 py-5 lg:px-6 lg:py-6"
        }
        data-testid={`assist-${show}`}
      >
        {show === "prepared" ? (
          <AssistWorkbench linked={params.get("item")} />
        ) : show === "suggestions" ? (
          <AssistSuggestionsView />
        ) : show === "workflows" ? (
          <AssistWorkflowsView />
        ) : show === "memory" ? (
          <AssistMemoryView />
        ) : show === "log" ? (
          <LogTab />
        ) : (
          <SwitchesTab />
        )}
      </div>
    </div>
  );
}

/** What Assist did for the person: the audit for the owner; each person's own journal is the work log. */
function LogTab() {
  const { data: area, error, retry } = useRead<AutopilotArea>("/api/autopilot", (signal) => getAutopilot(signal));
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!area) return <Pending page label="Reading what Assist did" />;
  return area.owner ? (
    <AuditView area={area} filters={NO_FILTERS} />
  ) : (
    <p className="text-[13px] text-muted" data-testid="assist-log-own">
      What Assist did for you is in your{" "}
      <Link href="/work-log" className="underline underline-offset-2">
        work log
      </Link>
      , newest first, with the time it saved.
    </p>
  );
}

function SwitchesTab() {
  const { data: area, error, retry, at } = useRead<AutopilotArea>("/api/autopilot", (signal) => getAutopilot(signal));
  useStaleRefresh(area?.stale_since, retry);
  useEffect(() => {
    const c = new AbortController();
    getTeamsConnection(c.signal).catch(() => undefined);
    getRecurring(c.signal).catch(() => undefined);
    return () => c.abort();
  }, []);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!area) return <Pending page label="Reading your switches" />;
  return (
    <>
      <StoredAge at={at} className="pb-1" />
      <FeedNote feed="autopilot" className="mb-2 text-[13px] text-risk" />
      <StaleNote since={area.stale_since} readAt={area.read_at} className="mb-2" />
      <AutopilotView area={area} onReload={retry} />
    </>
  );
}
