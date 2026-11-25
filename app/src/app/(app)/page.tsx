"use client";

import {
  Bell,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  ClipboardList,
  Clock3,
  ListChecks,
  Mail,
  MessageCircle,
  MessageSquare,
  MessageSquareWarning,
  Truck,
  X,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PreparedCard } from "@/components/autopilot/prepared";
import { noteParts } from "@/components/autopilot/lines";
import { HomeAsk } from "@/components/home-start";
import { CloseControl } from "@/components/late/close-control";
import { MobileAccount, MobileSearch, publishNeedsCount, SectionTabs } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { getHomeNeeds, type PreparedItem } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate, formatMoney, readable, safeHref } from "@/lib/format";
import { keyOwner } from "@/lib/home";
import { useStaleRefreshOnReturn } from "@/lib/stale-refresh";
import type { HomeNeedItem, HomeNeeds } from "@/lib/types";

const loadAlerts = () => import("@/components/home/alerts-view");
const loadLate = () => import("@/components/home/late-view");
const AlertsView = dynamic(() => loadAlerts().then((m) => m.AlertsView), {
  loading: () => <Pending label="Reading the alerts" />,
});
const LateView = dynamic(() => loadLate().then((m) => m.LateView), {
  loading: () => <Pending label="Reading the late POs" />,
});

function usePreloadHomeParts() {
  useEffect(() => {
    const idle = () => void Promise.all([loadAlerts(), loadLate()]).catch(() => undefined);
    const ric = (window as Window & { requestIdleCallback?: (f: () => void) => number }).requestIdleCallback;
    if (ric) ric(idle);
    else setTimeout(idle, 1500);
  }, []);
}

const PAGE_ROWS = 100;
const TZ = "America/Toronto";
const KIND_ORDER = ["reply", "approve", "question", "late_po", "alert", "task", "waiting", "routine", "gap", "feedback"];
const SOURCE_ORDER = ["mail", "teams", "pos", "alerts", "planner", "mc", "gaps", "feedback", "system"];
const DUE_ORDER = ["overdue", "today", "next7", "later", "undated"];

// guard-ok: display labels for typed kind values; no meaning is guessed from these words.
const KIND_WORDS: Record<string, string> = {
  reply: "Reply",
  approve: "Approve",
  question: "Question",
  task: "Task",
  waiting: "Waiting",
  alert: "Alert",
  late_po: "Late PO",
  routine: "Routine",
  feedback: "Feedback",
  gap: "Gap",
};
// guard-ok: display labels for typed source values; no meaning is guessed from these words.
const SOURCE_WORDS: Record<string, string> = {
  mail: "Mail",
  teams: "Teams",
  mc: "Mission Control",
  planner: "Planner",
  alerts: "Alerts",
  pos: "POs",
  feedback: "Feedback",
  gaps: "Gaps",
  system: "System",
};

function sourceLabelOf(item: Pick<HomeNeedItem, "source" | "source_label">): string {
  if (item.source === "mc" || item.source_label === "MC") return "Mission Control";
  return item.source_label || SOURCE_WORDS[item.source] || item.source;
}

function cleanBodyText(raw: string): string {
  const text = readable(raw, { shortDates: true }).trim();
  if (!text) return "";
  return /[.!?:…)]$/.test(text) ? text : `${text}.`;
}
// guard-ok: display labels for typed due-date buckets; no meaning is guessed from these words.
const DUE_WORDS: Record<string, string> = {
  overdue: "Overdue",
  today: "Due today",
  next7: "Next 7 days",
  later: "Later",
  undated: "No date",
};

type FilterState = { kinds: string[]; source: string; due: string };

function useDesktop() {
  const [desk, setDesk] = useState(true);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(min-width: 1024px)");
    const on = () => setDesk(media.matches);
    on();
    media.addEventListener("change", on);
    return () => media.removeEventListener("change", on);
  }, []);
  return desk;
}

function splitParam(text: string | null): string[] {
  return (text ?? "").split(",").map((x) => x.trim()).filter(Boolean);
}

function dayOf(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleDateString("en-CA", { timeZone: TZ });
}

function dueValue(item: HomeNeedItem): string {
  const dueDay = dayOf(item.due_at);
  if (!dueDay) return "undated";
  const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
  const days = Math.round(
    (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${dueDay}T12:00:00Z`).getTime()) / 86_400_000,
  );
  if (days > 0) return "overdue";
  if (days === 0) return "today";
  if (days >= -7) return "next7";
  return "later";
}

function whenWords(item: HomeNeedItem): string {
  const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
  const dueDay = dayOf(item.due_at);
  if (dueDay) {
    const days = Math.round(
      (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${dueDay}T12:00:00Z`).getTime()) / 86_400_000,
    );
    if (days > 0) return days === 1 ? "1 day overdue" : `${days} days overdue`;
    if (days === 0) return "Due today";
    return `Due ${formatDate(item.due_at, { short: true })}`;
  }
  if (item.days_late != null && item.days_late > 0) {
    return item.days_late === 1 ? "1 day overdue" : `${item.days_late} days overdue`;
  }
  const basis = dayOf(item.basis_at);
  if (!basis) return "";
  const days = Math.max(
    0,
    Math.round(
      (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${basis}T12:00:00Z`).getTime()) / 86_400_000,
    ),
  );
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return `${days} days old`;
}

function KindIcon({ item, className }: { item: HomeNeedItem; className?: string }) {
  const cls = cn("size-[16px] shrink-0 text-muted", className);
  if (item.kind === "late_po") return <Truck className={cls} aria-label="Late PO" />;
  if (item.kind === "alert") return <Bell className={cls} aria-label="Alert" />;
  if (item.kind === "approve") return <CheckCircle2 className={cls} aria-label="Approve" />;
  if (item.kind === "question") return <CircleHelp className={cls} aria-label="Question" />;
  if (item.kind === "waiting") return <Clock3 className={cls} aria-label="Waiting" />;
  if (item.kind === "feedback") return <MessageSquareWarning className={cls} aria-label="Feedback" />;
  if (item.kind === "gap") return <CircleHelp className={cls} aria-label="Gap" />;
  if (item.kind === "reply") {
    return item.source === "teams" ? <MessageSquare className={cls} aria-label="Teams" /> : <Mail className={cls} aria-label="Mail" />;
  }
  if (item.kind === "routine") return <ListChecks className={cls} aria-label="Routine" />;
  return <ClipboardList className={cls} aria-label="Task" />;
}

function NeedRow({ item, on, onPick }: { item: HomeNeedItem; on: boolean; onPick: () => void }) {
  const href = safeHref(item.href) ?? "/";
  const when = whenWords(item);
  const overdue = dueValue(item) === "overdue" || (item.days_late != null && item.days_late > 0);
  const srcLabel = sourceLabelOf(item);
  const titleText = readable(item.title, { shortDates: true });
  const parts = noteParts(item.line ?? "");
  const lineText = parts.text ? cleanBodyText(parts.text) : "";
  return (
    <li className="cv-auto cv-row">
      <a
        href={href}
        data-row-link=""
        data-testid="need-row"
        aria-current={on ? "true" : undefined}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey) return;
          e.preventDefault();
          onPick();
        }}
        className={cn(
          "flex gap-3 rounded-lg border px-3.5 py-3 outline-none transition-colors",
          on
            ? "border-accent/60 bg-row-on shadow-[var(--lift),inset_2px_0_0_var(--accent)]"
            : "border-border/60 bg-bg/70 hover:border-border-strong hover:bg-surface/80",
          "focus-visible:ring-2 focus-visible:ring-ring/50",
        )}
      >
        <span className="pt-0.5"><KindIcon item={item} /></span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 truncate text-[14px] font-medium leading-5 text-fg">{titleText}</span>
            {item.draft_ready && (
              <span className="shrink-0 rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[11px] font-medium leading-3.5 text-fg/85">
                Draft ready
              </span>
            )}
            {when && (
              <span
                className={cn(
                  "ml-auto shrink-0 tabular-nums",
                  overdue
                    ? "rounded-md border border-late-line bg-late-soft px-2 py-0.5 text-[11px] font-medium leading-3.5 text-late"
                    : "pl-2 text-[12px] leading-4 text-muted",
                )}
              >
                {when}
              </span>
            )}
          </span>
          {(item.person || srcLabel || item.action_label) && (
            <span className="mt-1 flex flex-wrap items-center gap-1 text-[12px] leading-4 text-muted">
              {item.person && <span className="truncate">{item.person}</span>}
              {item.person && srcLabel && <span aria-hidden> · </span>}
              {srcLabel && (
                <span className="inline-flex items-center rounded-md bg-surface-2 px-1.5 py-0.5 text-[11px] font-medium leading-3.5 text-muted">
                  {srcLabel}
                </span>
              )}
              {(item.person || srcLabel) && item.action_label && <span aria-hidden> · </span>}
              {item.action_label && <span className="truncate">{item.action_label}</span>}
            </span>
          )}
          {lineText && (
            <span className="mt-1.5 block truncate text-[13px] leading-5 text-fg/85" title={lineText}>
              {lineText}
            </span>
          )}
          {parts.labels.length > 0 && (
            <span className="mt-1.5 flex flex-wrap gap-1.5" data-testid="need-scope">
              {parts.labels.map((label) => (
                <span key={label} className="inline-flex items-center rounded-md bg-surface-2 px-1.5 py-0.5 text-[11px] leading-4 text-muted">
                  {label.replace(/\.$/, "")}
                </span>
              ))}
            </span>
          )}
        </span>
      </a>
    </li>
  );
}

function preparedOf(item: HomeNeedItem): PreparedItem | null {
  if (!item.matter_id || (!item.body && !item.ask)) return null;
  return {
    item_id: item.matter_id,
    sender: item.person ?? "",
    channel: item.source === "teams" ? "teams" : item.source === "mail" ? "mail" : "system",
    subject: item.title,
    message: item.quote ?? "",
    kind: item.body ? "reply" : "your_call",
    to: item.to ?? [],
    cc: [],
    body: item.body ?? "",
    edited: false,
    state: item.state === "unverified" ? "unverified" : "open",
    verified_at: item.verified_at,
    plan_status: item.plan_status,
    ask: item.ask ?? "",
    facts: [],
    created_at: item.basis_at ?? "",
  };
}

function MatterView({ item, onGone }: { item: HomeNeedItem; onGone: (id: string) => void }) {
  const prepared = preparedOf(item);
  if (!prepared) return null;
  return (
    <ul className="border-t border-border pt-2" data-testid="need-draft">
      <PreparedCard item={prepared} onGone={() => onGone(item.id)} onChanged={() => undefined} />
    </ul>
  );
}

function detailLinkLabel(item: HomeNeedItem): string {
  if (item.kind === "gap") return "Open in Data health";
  if (item.kind === "feedback") return "Open in Feedback";
  if (item.object_id) return "Open the full record";
  return "Open in Assist";
}

function NeedDetail({
  item,
  onClose,
  onGone,
}: {
  item: HomeNeedItem;
  onClose: () => void;
  onGone: (id: string) => void;
}) {
  const href = safeHref(item.href);
  const srcLabel = sourceLabelOf(item);
  const titleText = readable(item.title, { shortDates: true });
  const parts = noteParts(item.line ?? "");
  const lineText = parts.text ? cleanBodyText(parts.text) : "";
  const nextStep = !item.matter_id && item.ask ? readable(item.ask, { shortDates: true }).trim() : "";
  const subtitle = [
    item.person,
    whenWords(item),
    item.amount ? formatMoney(item.amount.amount, item.amount.currency) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const askHref = item.object_id
    ? `/ask?q=${encodeURIComponent(`What is the latest on ${item.title}?`)}&object=${encodeURIComponent(item.object_id)}`
    : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg" data-testid="need-detail">
      <div className="hidden h-[52px] shrink-0 items-center gap-2 border-b border-border px-5 lg:flex">
        <KindIcon item={item} />
        <span className="min-w-0 truncate text-[13px] text-muted">
          {KIND_WORDS[item.kind] ?? item.kind} · {srcLabel}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close details"
          title="Close (Esc)"
          data-testid="close-detail"
          className="ml-auto inline-flex size-8 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer"
        >
          <X className="size-4" aria-hidden />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6 sm:px-8">
        <div className="max-w-2xl space-y-5">
          <header className="space-y-1.5">
            <div className="flex items-center gap-2 text-[12px] text-muted lg:hidden">
              <KindIcon item={item} />
              <span>{KIND_WORDS[item.kind] ?? item.kind} · {srcLabel}</span>
            </div>
            <h2 className="text-[20px] font-semibold leading-7 tracking-[-0.015em] text-fg">{titleText}</h2>
            {subtitle && <p className="text-[13px] text-muted">{subtitle}</p>}
            {parts.labels.length > 0 && (
              <div className="flex flex-wrap gap-1.5 pt-0.5" data-testid="need-detail-scope">
                {parts.labels.map((label) => (
                  <span key={label} className="inline-flex items-center rounded-[4px] bg-hover px-2 py-0.5 text-[11.5px] leading-4 text-muted">
                    {label.replace(/\.$/, "")}
                  </span>
                ))}
              </div>
            )}
          </header>

          {lineText && <p className="text-[14px] leading-6 text-fg">{lineText}</p>}

          {nextStep && nextStep !== lineText && (
            <div className="rounded-lg border border-border bg-hover/50 px-4 py-3" data-testid="need-next-step">
              <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-muted">Suggested next step</p>
              <p className="mt-1 text-[13px] leading-5 text-fg">{nextStep}</p>
            </div>
          )}

          {item.quote && (
            <blockquote className="border-l-2 border-border pl-3 text-[13px] leading-5 text-muted whitespace-pre-wrap">
              {item.quote}
            </blockquote>
          )}

          {item.rank_reasons && item.rank_reasons.length > 0 && (
            <p className="text-[12px] text-muted">Here because it is {item.rank_reasons.join(", ")}.</p>
          )}

          {item.state !== "open" && (
            <p className="text-[12px] text-muted">
              {item.state === "unverified"
                ? "Its source could not be checked again; it stays listed until checked."
                : "It may already be closed or missing in the data; check it before acting."}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-4">
            {href && href !== "/" && (
              <Link href={href} className="text-[13px] font-medium text-accent underline-offset-2 hover:underline">
                {detailLinkLabel(item)}
              </Link>
            )}
            {askHref && (
              <Link href={askHref} className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-fg">
                <MessageCircle className="size-3.5" aria-hidden />
                Ask about this
              </Link>
            )}
          </div>

          {item.kind === "late_po" && item.object_id && (
            <div className="border-t border-border pt-3">
              <CloseControl order={{ object_id: item.object_id }} onChanged={() => onGone(item.id)} />
            </div>
          )}

          <MatterView item={item} onGone={onGone} />
        </div>
      </div>
    </div>
  );
}

function partLines(payload: HomeNeeds): string[] {
  const lines: string[] = [];
  if (payload.why) lines.push(payload.why);
  for (const part of payload.parts ?? []) {
    if (part.status !== "ok" && part.status !== "off" && part.note && !payload.why?.includes(part.note)) {
      lines.push(`${part.label}: ${part.note}`);
    }
  }
  return [...new Set(lines)];
}

function initialFilters(params: ReturnType<typeof useSearchParams>): FilterState {
  const kinds = splitParam(params?.get("kind") ?? null);
  if (kinds.length === 0 && params?.get("view") === "reply") kinds.push("reply");
  return {
    kinds,
    source: (params?.get("source") ?? "").trim(),
    due: (params?.get("due") ?? "").trim(),
  };
}

function Today() {
  const router = useRouter();
  const params = useSearchParams();
  const desktop = useDesktop();

  const [payload, setPayload] = useState<HomeNeeds | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [round, setRound] = useState(0);
  const [gone, setGone] = useState<Set<string>>(() => new Set());
  const [shown, setShown] = useState(PAGE_ROWS);
  const [olderOpen, setOlderOpen] = useState(false);
  const [filters, setFilters] = useState<FilterState>(() => initialFilters(params));
  const [picked, setPicked] = useState<string | null>(() => {
    const matter = params?.get("matter");
    return matter ? (matter.startsWith("matter:") ? matter : `matter:${matter}`) : (params?.get("sel") ?? null);
  });

  const supplier = params?.get("supplier") ?? null;
  const listRef = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getHomeNeeds(c.signal).then(
      (got) => {
        if (c.signal.aborted) return;
        setPayload(got);
      },
      (e) => {
        if (c.signal.aborted) return;
        setBusy(noticeKind(e) === "busy");
        setError(plainError(e));
        publishNeedsCount(null);
      },
    );
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load, round]);

  useEffect(() => {
    if (!supplier && payload) {
      publishNeedsCount(payload.status === "ok" ? payload.total : null);
    }
  }, [payload, supplier]);

  const refresh = useCallback(() => setRound((n) => n + 1), []);
  useStaleRefreshOnReturn(payload?.stale_since, refresh);

  const activeCurrent = useMemo(
    () => (payload?.items ?? []).filter((item) => !gone.has(item.id) && (!supplier || item.person === supplier)),
    [payload, gone, supplier],
  );
  const activeOlder = useMemo(
    () => (payload?.older_items ?? []).filter((item) => !gone.has(item.id) && (!supplier || item.person === supplier)),
    [payload, gone, supplier],
  );

  const kindCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of activeCurrent) counts.set(row.kind, (counts.get(row.kind) ?? 0) + 1);
    return counts;
  }, [activeCurrent]);

  const sourceCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of activeCurrent) counts.set(row.source, (counts.get(row.source) ?? 0) + 1);
    return counts;
  }, [activeCurrent]);

  const dueCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of activeCurrent) {
      const bucket = dueValue(row);
      counts.set(bucket, (counts.get(bucket) ?? 0) + 1);
    }
    return counts;
  }, [activeCurrent]);

  const matches = useCallback(
    (item: HomeNeedItem) =>
      (filters.kinds.length === 0 || filters.kinds.includes(item.kind)) &&
      (!filters.source || item.source === filters.source) &&
      (!filters.due || dueValue(item) === filters.due),
    [filters],
  );

  const items = useMemo(() => activeCurrent.filter(matches), [activeCurrent, matches]);
  const olderItems = useMemo(() => activeOlder.filter(matches), [activeOlder, matches]);

  useEffect(() => {
    setShown(PAGE_ROWS);
  }, [filters, supplier]);

  // guard-ok: exact total, Show more, and Show all controls are rendered beside the list.
  const visible = items.slice(0, shown);
  const selectable = useMemo(
    () => (olderOpen ? [...visible, ...olderItems] : visible),
    [visible, olderOpen, olderItems],
  );
  const allListed = useMemo(() => [...items, ...olderItems], [items, olderItems]);
  const selected = useMemo(
    () => (picked ? (allListed.find((item) => item.id === picked) ?? null) : null),
    [allListed, picked],
  );

  const showDetail = !desktop && !!picked && !!selected;
  const wide = desktop && !selected;

  const replaceUrl = useCallback((mutate: (q: URLSearchParams) => void) => {
    if (typeof window === "undefined") return;
    const q = new URLSearchParams(window.location.search);
    mutate(q);
    const qs = q.toString();
    window.history.replaceState(null, "", qs ? `/?${qs}` : "/");
  }, []);

  const pick = useCallback(
    (id: string) => {
      setPicked(id);
      replaceUrl((q) => {
        q.delete("matter");
        q.set("sel", id);
      });
    },
    [replaceUrl],
  );

  const closeDetail = useCallback(() => {
    setPicked(null);
    replaceUrl((q) => {
      q.delete("sel");
      q.delete("matter");
    });
  }, [replaceUrl]);

  const toggleKind = useCallback(
    (kind: string) => {
      setFilters((cur) => {
        const nextKinds = cur.kinds.includes(kind) ? cur.kinds.filter((x) => x !== kind) : [...cur.kinds, kind];
        replaceUrl((q) => {
          q.delete("view");
          if (nextKinds.length) q.set("kind", nextKinds.join(","));
          else q.delete("kind");
        });
        return { ...cur, kinds: nextKinds };
      });
    },
    [replaceUrl],
  );

  const setAllKinds = useCallback(() => {
    setFilters((cur) => ({ ...cur, kinds: [] }));
    replaceUrl((q) => {
      q.delete("view");
      q.delete("kind");
    });
  }, [replaceUrl]);

  const setSourceFilter = useCallback(
    (source: string) => {
      setFilters((cur) => ({ ...cur, source }));
      replaceUrl((q) => {
        if (source) q.set("source", source);
        else q.delete("source");
      });
    },
    [replaceUrl],
  );

  const setDueFilter = useCallback(
    (due: string) => {
      setFilters((cur) => ({ ...cur, due }));
      replaceUrl((q) => {
        if (due) q.set("due", due);
        else q.delete("due");
      });
    },
    [replaceUrl],
  );

  const clearFilters = useCallback(() => {
    setFilters({ kinds: [], source: "", due: "" });
    replaceUrl((q) => {
      q.delete("kind");
      q.delete("source");
      q.delete("due");
      q.delete("view");
    });
  }, [replaceUrl]);

  const markGone = useCallback(
    (id: string) => {
      setGone((cur) => new Set(cur).add(id));
      setPayload((cur) => {
        if (!cur) return cur;
        const inMain = (cur.items ?? []).some((row) => row.id === id);
        const inOlder = (cur.older_items ?? []).some((row) => row.id === id);
        if (!inMain && !inOlder) return cur;
        return {
          ...cur,
          items: (cur.items ?? []).filter((row) => row.id !== id),
          total: inMain ? Math.max(0, cur.total - 1) : cur.total,
          older_items: (cur.older_items ?? []).filter((row) => row.id !== id),
          older_total: inOlder ? Math.max(0, (cur.older_total ?? 0) - 1) : cur.older_total,
        };
      });
      if (picked === id) closeDetail();
    },
    [picked, closeDetail],
  );

  // Escape closes the open detail panel; j/k and arrow keys move through the list.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      if (e.key === "Escape" && picked) {
        const active = document.activeElement;
        if (active instanceof Element && active.closest("[role='dialog']")) return;
        e.preventDefault();
        closeDetail();
        return;
      }
      if (!desktop || selectable.length === 0) return;
      if (keyOwner(e.target) === "own") return;
      const at = selectable.findIndex((r) => r.id === picked);
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        pick(selectable[Math.min(selectable.length - 1, at + 1)].id);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        pick(selectable[Math.max(0, at <= 0 ? 0 : at - 1)].id);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [picked, closeDetail, desktop, selectable, pick]);

  const loading = !error && !payload;
  const notes = payload ? partLines(payload) : [];
  const hasFilters = filters.kinds.length > 0 || !!filters.source || !!filters.due;

  const kindChips = useMemo(() => {
    const present = new Set([...kindCounts.keys(), ...filters.kinds]);
    return [...KIND_ORDER.filter((k) => present.has(k)), ...[...present].filter((k) => !KIND_ORDER.includes(k))];
  }, [kindCounts, filters.kinds]);

  const sourceChoices = useMemo(() => {
    const present = new Set([...sourceCounts.keys(), ...(filters.source ? [filters.source] : [])]);
    return [...SOURCE_ORDER.filter((s) => present.has(s)), ...[...present].filter((s) => !SOURCE_ORDER.includes(s))];
  }, [sourceCounts, filters.source]);

  const dueChoices = useMemo(() => {
    const present = new Set([...dueCounts.keys(), ...(filters.due ? [filters.due] : [])]);
    return DUE_ORDER.filter((d) => present.has(d));
  }, [dueCounts, filters.due]);

  const showFilterBar =
    !loading &&
    !error &&
    (kindChips.length > 1 || sourceChoices.length > 1 || dueChoices.length > 1 || hasFilters);

  const list = (
    <section
      className={cn(
        "flex min-h-0 min-w-0 flex-1 flex-col border-border bg-list",
        wide ? "lg:max-w-none" : "lg:w-[384px] lg:flex-none lg:shrink-0 lg:border-r xl:w-[420px]",
        showDetail && "hidden",
      )}
      aria-label="Needs you"
      data-testid="briefing"
    >
      <header className="hidden h-[52px] shrink-0 items-center gap-3 border-b border-border px-5 lg:flex" data-testid="home-header">
        <h1 className="text-[15px] font-semibold tracking-[-0.015em]">Home</h1>
        {!loading && payload && payload.status === "ok" && (
          <span className="num text-[12px] text-muted" data-testid="needs-total">
            {items.length}
          </span>
        )}
      </header>
      <header className="lg:hidden">
        <div className="flex h-11 items-center justify-end gap-1 px-3">
          <MobileSearch />
          <MobileAccount />
        </div>
        <h1 className="px-5 text-[30px] leading-9 font-bold tracking-[-0.025em]">Home</h1>
      </header>
      <SectionTabs section="home" />
      <PageIntro path="/" className="px-5 pt-2 lg:px-5 lg:pt-3" />

      {supplier && (
        <div className="flex shrink-0 items-center gap-2 px-5 pt-3" data-testid="supplier-filter">
          <span className="inline-flex h-7 min-w-0 items-center gap-2 rounded-[4px] bg-bg pr-1 pl-2 shadow-lift">
            <span className="truncate text-[12px] font-medium">{supplier}</span>
            <button
              type="button"
              onClick={() => router.push("/")}
              aria-label="Show every supplier"
              className="flex size-5 items-center justify-center rounded text-muted hover:bg-hover hover:text-fg cursor-pointer"
            >
              <X className="size-3.5" aria-hidden />
            </button>
          </span>
        </div>
      )}

      {!supplier && <HomeAsk className="shrink-0 px-5 pt-3" />}

      {showFilterBar && (
        <div className="shrink-0 space-y-2 border-b border-border px-5 py-3" data-testid="needs-filters">
          {(kindChips.length > 1 || filters.kinds.length > 0) && (
            <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by type" data-testid="kind-filters">
              <button
                type="button"
                aria-pressed={filters.kinds.length === 0}
                onClick={setAllKinds}
                className={cn(
                  "inline-flex h-7 items-center gap-1 rounded-[4px] px-2 text-[12px] cursor-pointer",
                  filters.kinds.length === 0 ? "bg-row-on font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                )}
              >
                All
                <span className="num text-[11px] text-faint">{activeCurrent.length}</span>
              </button>
              {kindChips.map((kind) => {
                const on = filters.kinds.includes(kind);
                const count = kindCounts.get(kind) ?? 0;
                return (
                  <button
                    key={kind}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggleKind(kind)}
                    className={cn(
                      "inline-flex h-7 items-center gap-1 rounded-[4px] px-2 text-[12px] cursor-pointer",
                      on ? "bg-row-on font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    {KIND_WORDS[kind] ?? kind}
                    <span className="num text-[11px] text-faint">{count}</span>
                  </button>
                );
              })}
            </div>
          )}

          {(sourceChoices.length > 1 || dueChoices.length > 1 || hasFilters) && (
            <div className="flex flex-wrap items-center gap-2">
              {sourceChoices.length > 1 && (
                <label className="relative inline-flex h-7 items-center rounded-[4px] bg-bg/60 hover:bg-hover">
                  <select
                    aria-label="Filter by source"
                    value={filters.source}
                    onChange={(e) => setSourceFilter(e.target.value)}
                    className="h-full cursor-pointer appearance-none bg-transparent pr-6 pl-2 text-[12px] text-fg outline-none"
                  >
                    <option value="">All sources</option>
                    {sourceChoices.map((src) => (
                      <option key={src} value={src}>
                        {SOURCE_WORDS[src] ?? src} ({sourceCounts.get(src) ?? 0})
                      </option>
                    ))}
                  </select>
                  <ChevronDown className="pointer-events-none absolute right-1.5 size-3 text-muted" aria-hidden />
                </label>
              )}
              {dueChoices.length > 1 && (
                <label className="relative inline-flex h-7 items-center rounded-[4px] bg-bg/60 hover:bg-hover">
                  <select
                    aria-label="Filter by due date"
                    value={filters.due}
                    onChange={(e) => setDueFilter(e.target.value)}
                    className="h-full cursor-pointer appearance-none bg-transparent pr-6 pl-2 text-[12px] text-fg outline-none"
                  >
                    <option value="">Any timing</option>
                    {dueChoices.map((bucket) => (
                      <option key={bucket} value={bucket}>
                        {DUE_WORDS[bucket] ?? bucket} ({dueCounts.get(bucket) ?? 0})
                      </option>
                    ))}
                  </select>
                  <ChevronDown className="pointer-events-none absolute right-1.5 size-3 text-muted" aria-hidden />
                </label>
              )}
              {hasFilters && (
                <button
                  type="button"
                  onClick={clearFilters}
                  className="text-[12px] text-muted hover:text-fg cursor-pointer"
                  data-testid="clear-filters"
                >
                  Clear filters
                </button>
              )}
            </div>
          )}
        </div>
      )}

      <StaleNote since={payload?.stale_since} onReturn={!!payload?.stale_since} className="shrink-0 px-5 py-1.5" />
      {notes.length > 0 && (
        <div className="shrink-0 space-y-1 px-5 py-2 text-[12px] text-muted" data-testid="needs-state" aria-live="polite">
          {notes.map((line) => (
            <p key={line}>{line}</p>
          ))}
        </div>
      )}

      <div
        ref={listRef}
        className="min-h-0 flex-1 overflow-y-auto pb-4"
        data-testid="needs-you"
        {...(loading ? { "data-pending": "" } : {})}
      >
        {error ? (
          <div className="p-4">
            <LoadProblem message={error} busy={busy} onRetry={refresh} />
          </div>
        ) : loading ? (
          <div className="px-5 pt-4">
            <Pending label="Reading what needs you" />
          </div>
        ) : (
          <>
            {items.length === 0 ? (
              <div className="px-5 py-10 text-[13px] text-muted" data-testid="home-empty">
                {hasFilters
                  ? "Nothing matches these filters."
                  : payload?.status === "ok"
                    ? olderItems.length > 0
                      ? "Nothing recent needs you right now."
                      : "Nothing needs you right now."
                    : "The list could not be read completely; see the note above."}
              </div>
            ) : (
              <>
                <ul className="space-y-1.5 px-3 pt-2.5">
                  {visible.map((item) => (
                    <NeedRow key={item.id} item={item} on={selected?.id === item.id} onPick={() => pick(item.id)} />
                  ))}
                </ul>
                {visible.length < items.length && (
                  <div className="flex items-center gap-2 px-5 py-3 text-[12px] text-muted" data-testid="needs-paging">
                    <span>
                      Showing {visible.length} of {items.length}
                    </span>
                    <Button size="sm" variant="ghost" onClick={() => setShown((n) => n + PAGE_ROWS)}>
                      Show {Math.min(PAGE_ROWS, items.length - visible.length)} more
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setShown(items.length)}>
                      Show all
                    </Button>
                  </div>
                )}
              </>
            )}

            {olderItems.length > 0 && payload && (
              <div className="mt-2 border-t border-border px-2 pt-2" data-testid="older-needs">
                <button
                  type="button"
                  aria-expanded={olderOpen}
                  onClick={() => setOlderOpen((open) => !open)}
                  data-testid="older-needs-toggle"
                  className="flex w-full items-center gap-2 rounded-[4px] px-3 py-2 text-left text-[13px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
                >
                  {olderOpen ? (
                    <ChevronDown className="size-4 shrink-0" aria-hidden />
                  ) : (
                    <ChevronRight className="size-4 shrink-0" aria-hidden />
                  )}
                  <span>
                    Older than {payload.older_after_days} days ({olderItems.length})
                  </span>
                  <span className="ml-auto text-[12px] font-normal text-faint">{olderOpen ? "Hide" : "Show"}</span>
                </button>
                {olderOpen && (
                  <ul className="space-y-0.5 pt-1" data-testid="older-needs-list">
                    {olderItems.map((item) => (
                      <NeedRow key={item.id} item={item} on={selected?.id === item.id} onPick={() => pick(item.id)} />
                    ))}
                  </ul>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );

  return (
    <div className="flex h-full min-h-0 flex-1 lg:h-dvh">
      {list}
      {showDetail && selected && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="sticky top-0 z-20 flex h-11 shrink-0 items-center border-b border-border bg-bg px-1.5 lg:hidden">
            <button
              type="button"
              onClick={closeDetail}
              className="flex h-9 items-center gap-0.5 pr-2 text-[15px] text-accent cursor-pointer"
            >
              <ChevronLeft className="size-6" strokeWidth={2} aria-hidden />
              Home
            </button>
            <span className="ml-auto truncate pr-3 text-[13px] text-muted">{selected.title}</span>
          </div>
          <NeedDetail item={selected} onClose={closeDetail} onGone={markGone} />
        </div>
      )}
      {desktop && selected && (
        <div className="hidden min-w-0 flex-1 lg:flex">
          <NeedDetail key={selected.id} item={selected} onClose={closeDetail} onGone={markGone} />
        </div>
      )}
    </div>
  );
}

/** Home opens the unified Needs-you list; Alerts (?show=alerts) and Late POs (?show=late) remain intact. */
function HomeTab() {
  usePreloadHomeParts();
  const show = useSearchParams()?.get("show");
  if (show === "alerts") return <AlertsView />;
  if (show === "late") return <LateView />;
  return <Today />;
}

export default function HomePage() {
  return (
    <Suspense>
      <HomeTab />
    </Suspense>
  );
}
