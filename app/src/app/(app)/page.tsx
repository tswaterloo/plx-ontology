"use client";

import {
  ArrowUpRight,
  Bell,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  ClipboardList,
  Clock3,
  DollarSign,
  ListChecks,
  Loader2,
  Lock,
  Mail,
  MessageCircle,
  MessageSquare,
  MessageSquareWarning,
  RefreshCw,
  Server,
  ShieldAlert,
  Truck,
  X,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { canonicalObjectHref } from "@/components/autopilot/alert";
import { noteParts } from "@/components/autopilot/lines";
import { PreparedCard } from "@/components/autopilot/prepared";
import { CloseControl } from "@/components/late/close-control";
import { MobileAccount, MobileSearch, publishNeedsCount, SectionTabs } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { StyledSelect } from "@/components/ui/controls";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { decideAutopilotItem, getHomeNeeds, isObjectType, setWatcherItemState, type PreparedItem } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, readable, safeHref } from "@/lib/format";
import { keyOwner } from "@/lib/home";
import { useStaleRefreshOnReturn } from "@/lib/stale-refresh";
import { useRead } from "@/lib/use-read";
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

const STILL_OPEN_LEAD = /^Still open\?\s*Nothing has happened on it since it was due\s+([^.\n]+?)\.?\s*/i;
const ONLY_YOU_RE = /^Only you can see this\.?$/i;

/** Strip repetitive "Still open? Nothing has happened on it since it was due ..." boilerplate. */
function cleanBodyText(raw: string): { text: string; extractedDue: string | null } {
  const readableText = readable(raw, { shortDates: true }).trim();
  if (!readableText) return { text: "", extractedDue: null };
  let extractedDue: string | null = null;
  let body = readableText;
  const m = STILL_OPEN_LEAD.exec(body);
  if (m) {
    extractedDue = m[1].trim();
    body = body.slice(m[0].length).trim();
  }
  if (!body) return { text: "", extractedDue };
  body = body[0].toUpperCase() + body.slice(1);
  return { text: /[.!?:)]$/.test(body) ? body : `${body}.`, extractedDue };
}

/** Identify repetitive internal ops/system health alerts so business items lead the queue. */
function isOpsSystemNotice(item: HomeNeedItem): boolean {
  const title = (item.title ?? "").trim();
  if (/^(?:Workbench API health|API health|System health|Staging health|Health check)\b/i.test(title)) {
    return true;
  }
  if (item.source === "mail" && /:\s*ALERT\s*$/i.test(title) && /health|runbook|relations/i.test(`${title} ${item.line ?? ""}`)) {
    return true;
  }
  return false;
}

const FINANCIALS_EVENTS = new Set(["price_below_cost", "customer_price_drop", "customer_demand_drop", "price_below_typical"]);
const HOLD_EVENTS = new Set(["lot_on_hold", "quality_hold_aging", "qc_hold", "batch_hold", "order_hold"]);
// guard-ok: display badges for typed hold event values; no meaning is guessed from these words.
const HOLD_BADGES: Record<string, string> = {
  qc_hold: "QC hold",
  lot_on_hold: "Batch hold",
  batch_hold: "Batch hold",
  order_hold: "Order hold",
  quality_hold_aging: "Quality hold",
};

function isFinancialsItem(item: HomeNeedItem): boolean {
  return item.section === "financials" || FINANCIALS_EVENTS.has(item.event_type ?? "");
}

function isHoldItem(item: HomeNeedItem): boolean {
  return (
    item.section === "holds" ||
    item.alert_kind === "quality_hold" ||
    HOLD_EVENTS.has(item.event_type ?? "") ||
    HOLD_EVENTS.has(item.alert_kind ?? "")
  );
}

function itemBadgeOf(item: HomeNeedItem): string | null {
  if (item.badge) return item.badge;
  if (item.event_type && HOLD_BADGES[item.event_type]) return HOLD_BADGES[item.event_type];
  if (item.event_type === "customer_price_drop") return "Price drop";
  if (item.event_type === "customer_demand_drop") return "Demand drop";
  if (item.event_type === "price_below_cost") return "Below cost";
  if (item.event_type === "price_below_typical") return "Below typical price";
  if (isHoldItem(item)) return "Quality hold";
  return null;
}

function financialsSummaryLabel(items: HomeNeedItem[]): string {
  let priceDrops = 0;
  let demandDrops = 0;
  let belowCost = 0;
  let belowTypical = 0;
  let other = 0;
  for (const it of items) {
    if (it.event_type === "customer_price_drop") priceDrops += 1;
    else if (it.event_type === "customer_demand_drop") demandDrops += 1;
    else if (it.event_type === "price_below_cost") belowCost += 1;
    else if (it.event_type === "price_below_typical") belowTypical += 1;
    else other += 1;
  }
  const parts: string[] = [];
  if (priceDrops > 0) parts.push(`${priceDrops} ${priceDrops === 1 ? "price drop" : "price drops"}`);
  if (demandDrops > 0) parts.push(`${demandDrops} ${demandDrops === 1 ? "demand drop" : "demand drops"}`);
  if (belowCost > 0) parts.push(`${belowCost} below cost`);
  if (belowTypical > 0) parts.push(`${belowTypical} below typical`);
  if (other > 0) parts.push(`${other} other`);
  return parts.length > 0 ? `Financials · ${parts.join(", ")}` : `Financials (${items.length})`;
}

/** Resolve a canonical, always-loadable app route for a Home item. */
function resolveNeedHref(item: HomeNeedItem): string | null {
  const fallback = (): string | null => {
    if (item.object_id) {
      const colon = item.object_id.indexOf(":");
      const type = item.object_type || (colon > 0 ? item.object_id.slice(0, colon) : "");
      if (type && isObjectType(type)) return objectHref(type, item.object_id);
    }
    if (item.matter_id) return `/assist?item=${encodeURIComponent(item.matter_id)}`;
    return null;
  };
  const raw = safeHref(item.href);
  if (!raw || raw === "/") return fallback();
  if (raw.startsWith("/object/")) {
    const normalized = canonicalObjectHref(raw);
    const pathOnly = normalized.slice("/object/".length).split(/[?#]/, 1)[0];
    const parts = pathOnly.split("/").filter(Boolean);
    if (parts.length >= 2) {
      let kind = parts[0];
      try {
        kind = decodeURIComponent(parts[0]);
      } catch {
        // leave as-is
      }
      if (isObjectType(kind)) return normalized;
    }
    return fallback();
  }
  return raw;
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

function timingMeta(item: HomeNeedItem, extractedDue?: string | null): { badge: string; overdue: boolean; dueShort: string | null } {
  const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
  const dueDay = dayOf(item.due_at);
  if (dueDay) {
    const days = Math.round(
      (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${dueDay}T12:00:00Z`).getTime()) / 86_400_000,
    );
    const dueShort = formatDate(item.due_at, { short: true });
    if (days > 0) return { badge: `${days}d overdue`, overdue: true, dueShort };
    if (days === 0) return { badge: "Due today", overdue: false, dueShort: null };
    return { badge: `Due ${dueShort}`, overdue: false, dueShort: null };
  }
  if (item.days_late != null && item.days_late > 0) {
    return { badge: `${item.days_late}d overdue`, overdue: true, dueShort: extractedDue ?? null };
  }
  const basis = dayOf(item.basis_at);
  if (!basis) return { badge: "", overdue: false, dueShort: extractedDue ?? null };
  const days = Math.max(
    0,
    Math.round(
      (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${basis}T12:00:00Z`).getTime()) / 86_400_000,
    ),
  );
  if (days === 0) return { badge: "Today", overdue: false, dueShort: extractedDue ?? null };
  if (days === 1) return { badge: "Yesterday", overdue: false, dueShort: extractedDue ?? null };
  return { badge: `${days}d old`, overdue: false, dueShort: extractedDue ?? null };
}

function KindIcon({ item, className }: { item: HomeNeedItem; className?: string }) {
  const cls = cn("size-[15px] shrink-0 text-muted", className);
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

function MatterView({ item, onGone }: { item: HomeNeedItem; onGone: (id: string, note?: string) => void }) {
  const prepared = preparedOf(item);
  if (!prepared) return null;
  return (
    <ul className="border-t border-border pt-3" data-testid="need-draft">
      <PreparedCard item={prepared} onGone={() => onGone(item.id, `Updated ${readable(item.title, { shortDates: true })}`)} onChanged={() => undefined} />
    </ul>
  );
}

function detailLinkLabel(item: HomeNeedItem, resolvedHref: string | null): string {
  if (item.kind === "gap") return "Open in Data health";
  if (item.kind === "feedback") return "Open in Feedback";
  if (resolvedHref?.startsWith("/assist")) return "Open in Assist";
  if (item.object_id || resolvedHref?.startsWith("/object/") || resolvedHref?.startsWith("/po/") || resolvedHref?.startsWith("/order/")) {
    return "Open the full record";
  }
  return "Open in Assist";
}

function NeedInlineDetail({
  item,
  resolvedHref,
  onClose,
  onGone,
}: {
  item: HomeNeedItem;
  resolvedHref: string | null;
  onClose: () => void;
  onGone: (id: string, note?: string) => void;
}) {
  const srcLabel = sourceLabelOf(item);
  const titleText = readable(item.title, { shortDates: true });
  const parts = noteParts(item.line ?? "");
  const cleaned = cleanBodyText(parts.text);
  const timing = timingMeta(item, cleaned.extractedDue);
  const badge = itemBadgeOf(item);
  const nextStep = !item.matter_id && item.ask ? readable(item.ask, { shortDates: true }).trim() : "";
  const askHref = item.object_id
    ? `/ask?q=${encodeURIComponent(`What is the latest on ${item.title}?`)}&object=${encodeURIComponent(item.object_id)}`
    : `/ask?q=${encodeURIComponent(`What is the latest on ${titleText}?`)}`;
  const hasPreparedCard = !!preparedOf(item);
  const canDecideMatter = !!item.matter_id && !hasPreparedCard;
  const watcherId = item.id.startsWith("watcher:") ? item.id.slice("watcher:".length) : null;
  const canQuickAct = canDecideMatter || !!watcherId;
  const [acting, setActing] = useState<"snooze" | "dismiss" | null>(null);
  const [actError, setActError] = useState<string | null>(null);

  const quickAct = async (kind: "snooze" | "dismiss") => {
    if (acting) return;
    setActing(kind);
    setActError(null);
    const until = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
    try {
      if (canDecideMatter && item.matter_id) {
        await decideAutopilotItem(item.matter_id, kind === "snooze" ? { decision: "snooze", until } : { decision: "reject" });
      } else if (watcherId) {
        await setWatcherItemState(watcherId, kind === "snooze" ? "snoozed" : "dismissed", kind === "snooze" ? until : undefined);
      }
      onGone(item.id, kind === "snooze" ? `Snoozed ${titleText} for 1 day` : `Dismissed ${titleText}`);
    } catch (e) {
      setActError(plainError(e));
    } finally {
      setActing(null);
    }
  };

  return (
    <div
      className="border-t border-border/60 bg-surface/40 px-4 py-3.5 sm:pl-10"
      data-testid="need-detail"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-2.5">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12px] text-muted">
            <span className="font-medium text-fg">{KIND_WORDS[item.kind] ?? item.kind}</span>
            <span aria-hidden>·</span>
            <span>{srcLabel}</span>
            {badge && (
              <>
                <span aria-hidden>·</span>
                <span
                  data-testid="need-detail-badge"
                  className={cn(
                    "font-medium",
                    badge === "Verify unit cost" ? "text-risk" : "text-fg/85",
                  )}
                >
                  {badge}
                </span>
              </>
            )}
            {timing.badge && (
              <>
                <span aria-hidden>·</span>
                <span className={cn("num font-medium", timing.overdue ? "text-late/85" : "text-muted")}>
                  {timing.badge}
                  {timing.overdue && timing.dueShort ? ` · Due ${timing.dueShort}` : ""}
                </span>
              </>
            )}
            {item.person && (
              <>
                <span aria-hidden>·</span>
                <span className="text-fg/85">{item.person}</span>
              </>
            )}
            {item.amount && (
              <>
                <span aria-hidden>·</span>
                <span className="num font-medium text-fg">
                  {formatMoney(item.amount.amount, item.amount.currency)}
                </span>
              </>
            )}
            {item.action_label && (
              <>
                <span aria-hidden>·</span>
                <span>{item.action_label}</span>
              </>
            )}
          </div>

          <h2 className="text-[14.5px] font-semibold leading-5 tracking-[-0.01em] text-fg">{titleText}</h2>

          {cleaned.text && (
            <p className="text-[13.5px] leading-5 text-fg/90">{cleaned.text}</p>
          )}

          {(item.facts ?? []).length > 0 && (
            <ul className="space-y-1 text-[12.5px] leading-5 text-muted" data-testid="need-facts">
              {(item.facts ?? []).map((fact, idx) => (
                <li key={idx} className="flex items-start gap-2">
                  <span className="mt-2 size-1 shrink-0 rounded-full bg-border-strong" aria-hidden />
                  <span className="min-w-0">{readable(fact, { shortDates: true })}</span>
                </li>
              ))}
            </ul>
          )}

          {nextStep && nextStep !== cleaned.text && (
            <div className="border-l-2 border-accent/60 py-0.5 pl-3" data-testid="need-next-step">
              <p className="text-[11px] font-medium uppercase tracking-[0.04em] text-faint">Suggested next step</p>
              <p className="mt-0.5 text-[13px] leading-5 text-fg">{nextStep}</p>
            </div>
          )}

          {item.quote && (
            <blockquote className="border-l-2 border-border-strong py-0.5 pl-3 text-[13px] leading-5 text-muted whitespace-pre-wrap">
              {item.quote}
            </blockquote>
          )}

          {parts.labels.length > 0 && (
            <div className="flex flex-wrap items-center gap-3" data-testid="need-detail-scope">
              {parts.labels.map((label) => (
                <span key={label} className="inline-flex items-center gap-1 text-[12px] leading-4 text-faint">
                  {ONLY_YOU_RE.test(label.trim()) && <Lock className="size-3 text-faint" aria-hidden />}
                  {label.replace(/\.$/, "")}
                </span>
              ))}
            </div>
          )}

          {item.rank_reasons && item.rank_reasons.length > 0 && (
            <p className="text-[12px] text-faint">Here because it is {item.rank_reasons.join(", ")}.</p>
          )}

          {item.state !== "open" && (
            <p className="border-l-2 border-l-risk/70 pl-3 py-0.5 text-[12px] text-muted">
              {item.state === "unverified"
                ? "Its source could not be checked again; it stays listed until checked."
                : "It may already be closed or missing in the data; check it before acting."}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2 pt-1">
            {resolvedHref && resolvedHref !== "/" && (
              <Link
                href={resolvedHref}
                className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border-strong bg-bg px-2.5 text-[12.5px] font-medium text-fg transition-[background-color,border-color,transform] duration-150 hover:border-control hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98]"
              >
                {detailLinkLabel(item, resolvedHref)}
                <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
              </Link>
            )}
            <Link
              href={askHref}
              className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-transparent px-2.5 text-[12.5px] font-medium text-muted transition-[background-color,border-color,color,transform] duration-150 hover:border-border-strong hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98]"
            >
              <MessageCircle className="size-3.5" aria-hidden />
              Ask about this
            </Link>
            {canQuickAct && (
              <>
                <Button
                  type="button"
                  variant="ghost"
                  size="xs"
                  loading={acting === "snooze"}
                  disabled={acting !== null}
                  onClick={() => void quickAct("snooze")}
                  data-testid="need-snooze"
                >
                  <Clock3 className="size-3.5" aria-hidden />
                  {acting === "snooze" ? "Snoozing" : "Snooze 1 day"}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="xs"
                  loading={acting === "dismiss"}
                  disabled={acting !== null}
                  onClick={() => void quickAct("dismiss")}
                  data-testid="need-dismiss"
                >
                  {acting === "dismiss" ? "Dismissing" : "Dismiss"}
                </Button>
              </>
            )}
          </div>
          {actError && (
            <p role="alert" className="text-[12px] text-late" data-testid="need-act-error">
              {actError}
            </p>
          )}

          {item.kind === "late_po" && item.object_id && (
            <div className="border-t border-border pt-2.5">
              <CloseControl order={{ object_id: item.object_id }} onChanged={() => onGone(item.id, `Closed ${titleText}`)} />
            </div>
          )}

          <MatterView item={item} onGone={onGone} />
        </div>

        <button
          type="button"
          onClick={onClose}
          aria-label="Close details"
          title="Close (Esc)"
          data-testid="close-detail"
          className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-faint transition-colors hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] cursor-pointer"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>
    </div>
  );
}

function NeedRow({
  item,
  on,
  onPick,
  onClose,
  onGone,
}: {
  item: HomeNeedItem;
  on: boolean;
  onPick: () => void;
  onClose: () => void;
  onGone: (id: string, note?: string) => void;
}) {
  const resolvedHref = resolveNeedHref(item);
  const href = resolvedHref ?? "/";
  const srcLabel = sourceLabelOf(item);
  const titleText = readable(item.title, { shortDates: true });
  const parts = noteParts(item.line ?? "");
  const cleaned = cleanBodyText(parts.text);
  const timing = timingMeta(item, cleaned.extractedDue);
  const badge = itemBadgeOf(item);
  const isPrivate = parts.labels.some((l) => ONLY_YOU_RE.test(l.trim()));
  const otherLabels = parts.labels.filter((l) => !ONLY_YOU_RE.test(l.trim()));

  return (
    <li className="cv-auto">
      <div
        className={cn(
          "group relative flex items-start gap-2.5 px-3 py-2.5 transition-colors sm:px-4",
          on
            ? "bg-row-on before:absolute before:inset-y-0 before:left-0 before:w-[2px] before:bg-accent"
            : "hover:bg-hover",
        )}
      >
        <a
          href={href}
          data-row-link=""
          data-testid="need-row"
          aria-current={on ? "true" : undefined}
          aria-expanded={on}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            if (on) onClose();
            else onPick();
          }}
          className="flex min-w-0 flex-1 items-start gap-2.5 outline-none focus-visible:ring-2 focus-visible:ring-ring/50 rounded-xs cursor-pointer"
        >
          <span className="pt-0.5">
            <KindIcon item={item} className={on ? "text-fg" : undefined} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 items-baseline gap-2">
              <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium leading-5 text-fg" title={titleText}>
                {titleText}
              </span>
              {badge && (
                <span
                  data-testid="need-badge"
                  className={cn(
                    "shrink-0 text-[11.5px] font-medium leading-4",
                    badge === "Verify unit cost" ? "text-risk" : "text-muted",
                  )}
                >
                  {badge}
                </span>
              )}
              {item.draft_ready && (
                <span className="shrink-0 text-[11.5px] font-medium leading-4 text-accent">
                  Draft ready
                </span>
              )}
              {timing.badge && (
                <span
                  className={cn(
                    "num shrink-0 pl-1.5 text-[12px] leading-4 whitespace-nowrap",
                    timing.overdue ? "font-medium text-late/85" : "text-faint",
                  )}
                >
                  {timing.badge}
                </span>
              )}
            </span>
            <span className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5 text-[12px] leading-[18px] text-muted sm:flex-nowrap">
              {item.person && (
                <span className="shrink-0 truncate max-w-[180px]" title={item.person}>
                  {item.person}
                </span>
              )}
              {item.person && srcLabel && <span className="shrink-0" aria-hidden> · </span>}
              {srcLabel && <span className="shrink-0">{srcLabel}</span>}
              {(item.person || srcLabel) && item.action_label && <span className="shrink-0" aria-hidden> · </span>}
              {item.action_label && <span className="shrink-0">{item.action_label}</span>}
              {timing.overdue && timing.dueShort && (
                <>
                  <span className="shrink-0" aria-hidden> · </span>
                  <span className="num shrink-0 text-faint">Due {timing.dueShort}</span>
                </>
              )}
              {isPrivate && (
                <span
                  className="ml-0.5 inline-flex shrink-0 items-center text-faint"
                  title="Only you can see this"
                  aria-label="Only you can see this"
                  data-testid="need-private"
                >
                  <Lock className="size-3" strokeWidth={2} aria-hidden />
                </span>
              )}
              {cleaned.text && (
                <>
                  <span aria-hidden className="shrink-0 text-faint"> · </span>
                  <span className="min-w-0 flex-1 truncate text-faint" title={cleaned.text}>
                    {cleaned.text}
                  </span>
                </>
              )}
            </span>
            {otherLabels.length > 0 && (
              <span className="mt-0.5 flex flex-wrap gap-2 text-[11.5px] text-faint" data-testid="need-scope">
                {otherLabels.map((label) => (
                  <span key={label}>{label.replace(/\.$/, "")}</span>
                ))}
              </span>
            )}
          </span>
        </a>
        <div className="mt-0.5 flex shrink-0 items-center gap-0.5">
          {resolvedHref && resolvedHref !== "/" && (
            <Link
              href={resolvedHref}
              aria-label={`Open ${titleText}`}
              title="Open record"
              className="hidden size-6 shrink-0 items-center justify-center rounded-sm text-faint opacity-0 transition-[opacity,background-color,color] duration-150 hover:bg-hover hover:text-fg focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 group-hover:opacity-100 sm:inline-flex"
            >
              <ArrowUpRight className="size-3.5" strokeWidth={2} aria-hidden />
            </Link>
          )}
          <span className="inline-flex size-5 items-center justify-center text-faint transition-colors group-hover:text-muted" aria-hidden>
            {on ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          </span>
        </div>
      </div>
      {on && (
        <NeedInlineDetail item={item} resolvedHref={resolvedHref} onClose={onClose} onGone={onGone} />
      )}
    </li>
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

  const needsRead = useRead<HomeNeeds>("/api/home/needs", (signal) => getHomeNeeds(signal));
  const payload = needsRead.data;
  const readError = needsRead.error;
  const error = readError ? plainError(readError) : null;
  const busy = readError ? noticeKind(readError) === "busy" : false;
  const setPayload = needsRead.set;
  const [gone, setGone] = useState<Set<string>>(() => new Set());
  const [shown, setShown] = useState(PAGE_ROWS);
  const [holdsOpen, setHoldsOpen] = useState(true);
  const [financialsOpen, setFinancialsOpen] = useState(false);
  const [financialsShown, setFinancialsShown] = useState(PAGE_ROWS);
  const [olderOpen, setOlderOpen] = useState(false);
  const [opsOpen, setOpsOpen] = useState(false);
  const [filters, setFilters] = useState<FilterState>(() => initialFilters(params));
  const [picked, setPicked] = useState<string | null>(() => {
    const matter = params?.get("matter");
    return matter ? (matter.startsWith("matter:") ? matter : `matter:${matter}`) : (params?.get("sel") ?? null);
  });
  const [statusNote, setStatusNote] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const supplier = params?.get("supplier") ?? null;
  const listRef = useRef<HTMLDivElement>(null);

  const paramKey = params?.toString() ?? "";
  const mountedParams = useRef(paramKey);
  useEffect(() => {
    if (paramKey === mountedParams.current) return;
    mountedParams.current = paramKey;
    setFilters(initialFilters(params));
    const matter = params?.get("matter");
    setPicked(matter ? (matter.startsWith("matter:") ? matter : `matter:${matter}`) : (params?.get("sel") ?? null));
  }, [paramKey, params]);

  useEffect(() => {
    if (!statusNote) return;
    const t = window.setTimeout(() => setStatusNote(null), 5000);
    return () => window.clearTimeout(t);
  }, [statusNote]);

  useEffect(() => {
    if (payload || readError) setRefreshing(false);
  }, [payload, readError]);

  useEffect(() => {
    if (!supplier) {
      if (payload) {
        if (payload.status === "ok") {
          const foldedFinancials = (payload.items ?? []).filter((row) => !gone.has(row.id) && isFinancialsItem(row)).length;
          publishNeedsCount(Math.max(0, payload.total - foldedFinancials));
        } else {
          publishNeedsCount(null);
        }
      } else if (readError) {
        publishNeedsCount(null);
      }
    }
  }, [payload, readError, supplier, gone]);

  const refresh = needsRead.retry;
  useStaleRefreshOnReturn(payload?.stale_since, refresh);

  const activeCurrent = useMemo(
    () => (payload?.items ?? []).filter((item) => !gone.has(item.id) && (!supplier || item.person === supplier)),
    [payload, gone, supplier],
  );
  const activeOlder = useMemo(
    () => (payload?.older_items ?? []).filter((item) => !gone.has(item.id) && (!supplier || item.person === supplier)),
    [payload, gone, supplier],
  );

  const unfoldedCurrent = useMemo(
    () => activeCurrent.filter((row) => !isFinancialsItem(row)),
    [activeCurrent],
  );

  const kindCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of unfoldedCurrent) counts.set(row.kind, (counts.get(row.kind) ?? 0) + 1);
    return counts;
  }, [unfoldedCurrent]);

  const sourceCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of unfoldedCurrent) counts.set(row.source, (counts.get(row.source) ?? 0) + 1);
    return counts;
  }, [unfoldedCurrent]);

  const dueCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of unfoldedCurrent) {
      const bucket = dueValue(row);
      counts.set(bucket, (counts.get(bucket) ?? 0) + 1);
    }
    return counts;
  }, [unfoldedCurrent]);

  const matches = useCallback(
    (item: HomeNeedItem) =>
      (filters.kinds.length === 0 || filters.kinds.includes(item.kind)) &&
      (!filters.source || item.source === filters.source) &&
      (!filters.due || dueValue(item) === filters.due),
    [filters],
  );

  const items = useMemo(() => activeCurrent.filter(matches), [activeCurrent, matches]);
  const olderItems = useMemo(() => activeOlder.filter(matches), [activeOlder, matches]);

  // Separate repetitive internal ops notices, consolidated holds, and compact Financials alerts
  // so urgent business items lead while every single item remains reachable.
  const { businessItems, holdItems, financialsItems, opsNotices } = useMemo(() => {
    const ops = items.filter(isOpsSystemNotice);
    const opsIds = ops.length >= 2 ? new Set(ops.map((x) => x.id)) : new Set<string>();
    const regular: HomeNeedItem[] = [];
    const holds: HomeNeedItem[] = [];
    const financials: HomeNeedItem[] = [];
    for (const x of items) {
      if (opsIds.has(x.id)) continue;
      if (isFinancialsItem(x)) financials.push(x);
      else if (isHoldItem(x)) holds.push(x);
      else regular.push(x);
    }
    return {
      businessItems: regular,
      holdItems: holds,
      financialsItems: financials,
      opsNotices: ops.length >= 2 ? ops : ([] as HomeNeedItem[]),
    };
  }, [items]);

  const hasPrivateItems = useMemo(
    () => items.some((row) => noteParts(row.line ?? "").labels.some((l) => ONLY_YOU_RE.test(l.trim()))),
    [items],
  );

  useEffect(() => {
    setShown(PAGE_ROWS);
    setFinancialsShown(PAGE_ROWS);
  }, [filters, supplier]);

  // guard-ok: exact total, Show more, and Show all controls are rendered beside the list.
  const visibleBusiness = businessItems.slice(0, shown);
  // guard-ok: exact total, Show more, and Show all controls are rendered inside the expanded Financials block.
  const visibleFinancials = financialsItems.slice(0, financialsShown);
  const selectable = useMemo(
    () => [
      ...visibleBusiness,
      ...(holdsOpen ? holdItems : []),
      ...(financialsOpen ? visibleFinancials : []),
      ...(opsOpen ? opsNotices : []),
      ...(olderOpen ? olderItems : []),
    ],
    [visibleBusiness, holdsOpen, holdItems, financialsOpen, visibleFinancials, opsOpen, opsNotices, olderOpen, olderItems],
  );
  const allListed = useMemo(() => [...items, ...olderItems], [items, olderItems]);

  const selected = useMemo(
    () => (picked ? (allListed.find((item) => item.id === picked) ?? null) : null),
    [allListed, picked],
  );

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
    (id: string, note?: string) => {
      setGone((cur) => new Set(cur).add(id));
      const cur = payload;
      if (cur) {
        const inMain = (cur.items ?? []).some((row) => row.id === id);
        const inOlder = (cur.older_items ?? []).some((row) => row.id === id);
        if (inMain || inOlder) {
          setPayload({
            ...cur,
            items: (cur.items ?? []).filter((row) => row.id !== id),
            total: inMain ? Math.max(0, cur.total - 1) : cur.total,
            older_items: (cur.older_items ?? []).filter((row) => row.id !== id),
            older_total: inOlder ? Math.max(0, (cur.older_total ?? 0) - 1) : cur.older_total,
          });
        }
      }
      if (picked === id) closeDetail();
      if (note) setStatusNote(note);
    },
    [payload, picked, setPayload, closeDetail],
  );

  // Escape collapses the expanded row; j/k and arrow keys step through rows.
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
      if (selectable.length === 0) return;
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
  }, [picked, closeDetail, selectable, pick]);

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

  const renderRow = (item: HomeNeedItem) => (
    <NeedRow
      key={item.id}
      item={item}
      on={selected?.id === item.id}
      onPick={() => pick(item.id)}
      onClose={closeDetail}
      onGone={markGone}
    />
  );

  return (
    <section
      className="flex min-h-0 flex-1 flex-col bg-bg"
      aria-label="Needs you"
      data-testid="briefing"
    >
      <header className="hidden h-12 shrink-0 items-center gap-2.5 border-b border-border bg-bg px-4 lg:flex lg:px-5" data-testid="home-header">
        <h1 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">Home</h1>
        {!loading && payload && payload.status === "ok" && (
          <span className="num text-[12px] font-medium text-muted" data-testid="needs-total">
            {hasFilters ? items.length : Math.max(0, items.length - financialsItems.length)}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {hasPrivateItems && (
            <span className="inline-flex items-center gap-1 text-[11.5px] text-faint" title="Items marked with a lock are visible only to you">
              <Lock className="size-3" strokeWidth={2} aria-hidden />
              <span className="hidden sm:inline">Private items marked with lock</span>
            </span>
          )}
          <button
            type="button"
            disabled={loading || refreshing}
            onClick={() => {
              setRefreshing(true);
              refresh();
            }}
            aria-label="Refresh Home"
            title="Refresh"
            data-testid="home-refresh"
            className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] text-muted transition-[background-color,color,transform] duration-150 hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50 cursor-pointer"
          >
            <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} aria-hidden />
            <span className="hidden xl:inline">{refreshing ? "Refreshing" : "Refresh"}</span>
          </button>
        </div>
      </header>
      <header className="bg-bg lg:hidden">
        <div className="flex h-11 items-center justify-end gap-1 px-3">
          <MobileSearch />
          <MobileAccount />
        </div>
        <h1 className="px-4 pb-1 text-[26px] leading-8 font-bold tracking-[-0.025em]">Home</h1>
      </header>
      <SectionTabs section="home" />
      <PageIntro path="/" />

      <div
        ref={listRef}
        className="min-h-0 flex-1 overflow-y-auto"
        data-testid="needs-you"
        {...(loading ? { "data-pending": "" } : {})}
      >
        <div className="mx-auto w-full max-w-[980px] px-4 pb-12 sm:px-6">
          {(supplier || showFilterBar) && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2.5">
              {supplier && (
                <div className="flex items-center gap-2" data-testid="supplier-filter">
                  <span className="inline-flex h-7 min-w-0 items-center gap-2 rounded-md border border-border bg-surface pr-1 pl-2.5">
                    <span className="truncate text-[12px] font-medium text-fg">{supplier}</span>
                    <button
                      type="button"
                      onClick={() => router.push("/")}
                      aria-label="Show every supplier"
                      className="flex size-5 items-center justify-center rounded-sm text-muted hover:bg-hover hover:text-fg cursor-pointer"
                    >
                      <X className="size-3.5" aria-hidden />
                    </button>
                  </span>
                </div>
              )}

              {showFilterBar && (
                <div className="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-2" data-testid="needs-filters">
                  {(kindChips.length > 1 || filters.kinds.length > 0) && (
                    <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Filter by type" data-testid="kind-filters">
                      <button
                        type="button"
                        aria-pressed={filters.kinds.length === 0}
                        onClick={setAllKinds}
                        className={cn(
                          "inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[12.5px] transition-colors cursor-pointer",
                          filters.kinds.length === 0
                            ? "bg-surface-2 font-medium text-fg"
                            : "text-muted hover:bg-hover hover:text-fg",
                        )}
                      >
                        All
                        <span className="num text-[11px] text-faint">{unfoldedCurrent.length}</span>
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
                              "inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[12.5px] transition-colors cursor-pointer",
                              on
                                ? "bg-surface-2 font-medium text-fg"
                                : "text-muted hover:bg-hover hover:text-fg",
                            )}
                          >
                            {KIND_WORDS[kind] ?? kind}
                            <span className={cn("num text-[11px]", on ? "text-muted" : "text-faint")}>{count}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {(sourceChoices.length > 1 || dueChoices.length > 1 || hasFilters) && (
                    <div className="flex flex-wrap items-center gap-1.5">
                      {sourceChoices.length > 1 && (
                        <StyledSelect
                          ariaLabel="Filter by source"
                          value={filters.source}
                          onChange={setSourceFilter}
                          options={[
                            { value: "", label: "All sources" },
                            ...sourceChoices.map((src) => ({
                              value: src,
                              label: `${SOURCE_WORDS[src] ?? src} (${sourceCounts.get(src) ?? 0})`,
                            })),
                          ]}
                        />
                      )}
                      {dueChoices.length > 1 && (
                        <StyledSelect
                          ariaLabel="Filter by due date"
                          value={filters.due}
                          onChange={setDueFilter}
                          options={[
                            { value: "", label: "Any timing" },
                            ...dueChoices.map((bucket) => ({
                              value: bucket,
                              label: `${DUE_WORDS[bucket] ?? bucket} (${dueCounts.get(bucket) ?? 0})`,
                            })),
                          ]}
                        />
                      )}
                      {hasFilters && (
                        <button
                          type="button"
                          onClick={clearFilters}
                          className="h-7 rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
                          data-testid="clear-filters"
                        >
                          Clear filters
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          <StaleNote since={payload?.stale_since} onReturn={!!payload?.stale_since} className="py-2" />
          {statusNote && (
            <div
              role="status"
              data-testid="home-action-status"
              className="mt-2 flex items-center justify-between gap-2 rounded-md border border-border bg-surface/60 px-3 py-2 text-[12.5px] text-fg"
            >
              <span className="flex min-w-0 items-center gap-2">
                <Check className="size-3.5 shrink-0 text-ok" strokeWidth={2.2} aria-hidden />
                <span className="truncate">{statusNote}</span>
              </span>
              <button
                type="button"
                aria-label="Dismiss notice"
                onClick={() => setStatusNote(null)}
                className="inline-flex size-5 shrink-0 items-center justify-center rounded-sm text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                <X className="size-3" aria-hidden />
              </button>
            </div>
          )}
          {notes.length > 0 && (
            <div className="space-y-0.5 border-b border-border py-2 text-[12px] text-muted" data-testid="needs-state" aria-live="polite">
              {notes.map((line) => (
                <p key={line}>{line}</p>
              ))}
            </div>
          )}

          {error ? (
            <div className="py-4">
              <LoadProblem message={error} busy={busy} onRetry={refresh} />
            </div>
          ) : loading ? (
            <div className="py-6">
              <Pending label="Reading what needs you" />
            </div>
          ) : (
            <>
              {items.length === 0 ? (
                <div className="py-12 text-center text-[13px] text-muted" data-testid="home-empty">
                  <p>
                    {hasFilters
                      ? "Nothing matches these filters."
                      : payload?.status === "ok"
                        ? olderItems.length > 0
                          ? "Nothing recent needs you right now."
                          : "Nothing needs you right now."
                        : "The list could not be read completely; see the note above."}
                  </p>
                  {hasFilters && (
                    <div className="mt-3">
                      <Button size="sm" variant="secondary" onClick={clearFilters}>
                        Clear filters
                      </Button>
                    </div>
                  )}
                </div>
              ) : (
                <>
                  {visibleBusiness.length > 0 && (
                    <ul className="divide-y divide-border border-b border-border">
                      {visibleBusiness.map(renderRow)}
                    </ul>
                  )}
                  {visibleBusiness.length < businessItems.length && (
                    <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-[12px] text-muted sm:px-4" data-testid="needs-paging">
                      <span className="num">
                        Showing {visibleBusiness.length} of {businessItems.length}
                      </span>
                      <Button size="xs" variant="ghost" onClick={() => setShown((n) => n + PAGE_ROWS)}>
                        Show {Math.min(PAGE_ROWS, businessItems.length - visibleBusiness.length)} more
                      </Button>
                      <Button size="xs" variant="ghost" onClick={() => setShown(businessItems.length)}>
                        Show all
                      </Button>
                    </div>
                  )}

                  {holdItems.length > 0 && (
                    <div className="border-b border-border" data-testid="holds-group">
                      <button
                        type="button"
                        aria-expanded={holdsOpen}
                        onClick={() => setHoldsOpen((open) => !open)}
                        data-testid="holds-toggle"
                        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg sm:px-4 cursor-pointer"
                      >
                        {holdsOpen ? (
                          <ChevronDown className="size-3.5 shrink-0" aria-hidden />
                        ) : (
                          <ChevronRight className="size-3.5 shrink-0" aria-hidden />
                        )}
                        <ShieldAlert className="size-3.5 shrink-0 text-faint" aria-hidden />
                        <span>Holds ({holdItems.length})</span>
                        <span className="ml-auto text-[11.5px] font-normal text-faint">{holdsOpen ? "Hide" : "Show"}</span>
                      </button>
                      {holdsOpen && (
                        <ul className="divide-y divide-border border-t border-border" data-testid="holds-list">
                          {holdItems.map(renderRow)}
                        </ul>
                      )}
                    </div>
                  )}

                  {financialsItems.length > 0 && (
                    <div className="border-b border-border" data-testid="financials-group">
                      <button
                        type="button"
                        aria-expanded={financialsOpen}
                        onClick={() => setFinancialsOpen((open) => !open)}
                        data-testid="financials-toggle"
                        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg sm:px-4 cursor-pointer"
                      >
                        {financialsOpen ? (
                          <ChevronDown className="size-3.5 shrink-0" aria-hidden />
                        ) : (
                          <ChevronRight className="size-3.5 shrink-0" aria-hidden />
                        )}
                        <DollarSign className="size-3.5 shrink-0 text-faint" aria-hidden />
                        <span className="min-w-0 truncate">{financialsSummaryLabel(financialsItems)}</span>
                        <span className="ml-auto shrink-0 text-[11.5px] font-normal text-faint">{financialsOpen ? "Hide" : "Show"}</span>
                      </button>
                      {financialsOpen && (
                        <>
                          <ul className="divide-y divide-border border-t border-border" data-testid="financials-list">
                            {visibleFinancials.map(renderRow)}
                          </ul>
                          {visibleFinancials.length < financialsItems.length && (
                            <div className="flex items-center gap-2 border-t border-border px-3 py-2 text-[12px] text-muted sm:px-4" data-testid="financials-paging">
                              <span className="num">
                                Showing {visibleFinancials.length} of {financialsItems.length}
                              </span>
                              <Button size="xs" variant="ghost" onClick={() => setFinancialsShown((n) => n + PAGE_ROWS)}>
                                Show {Math.min(PAGE_ROWS, financialsItems.length - visibleFinancials.length)} more
                              </Button>
                              <Button size="xs" variant="ghost" onClick={() => setFinancialsShown(financialsItems.length)}>
                                Show all
                              </Button>
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  )}

                  {opsNotices.length > 0 && (
                    <div className="border-b border-border" data-testid="ops-notices-group">
                      <button
                        type="button"
                        aria-expanded={opsOpen}
                        onClick={() => setOpsOpen((open) => !open)}
                        data-testid="ops-notices-toggle"
                        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg sm:px-4 cursor-pointer"
                      >
                        {opsOpen ? (
                          <ChevronDown className="size-3.5 shrink-0" aria-hidden />
                        ) : (
                          <ChevronRight className="size-3.5 shrink-0" aria-hidden />
                        )}
                        <Server className="size-3.5 shrink-0 text-faint" aria-hidden />
                        <span>System and ops notices ({opsNotices.length})</span>
                        <span className="ml-auto text-[11.5px] font-normal text-faint">{opsOpen ? "Hide" : "Show"}</span>
                      </button>
                      {opsOpen && (
                        <ul className="divide-y divide-border border-t border-border" data-testid="ops-notices-list">
                          {opsNotices.map(renderRow)}
                        </ul>
                      )}
                    </div>
                  )}
                </>
              )}

              {olderItems.length > 0 && payload && (
                <div className="border-b border-border" data-testid="older-needs">
                  <button
                    type="button"
                    aria-expanded={olderOpen}
                    onClick={() => setOlderOpen((open) => !open)}
                    data-testid="older-needs-toggle"
                    className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg sm:px-4 cursor-pointer"
                  >
                    {olderOpen ? (
                      <ChevronDown className="size-3.5 shrink-0" aria-hidden />
                    ) : (
                      <ChevronRight className="size-3.5 shrink-0" aria-hidden />
                    )}
                    <span>
                      Older than {payload.older_after_days} days ({olderItems.length})
                    </span>
                    <span className="ml-auto text-[11.5px] font-normal text-faint">{olderOpen ? "Hide" : "Show"}</span>
                  </button>
                  {olderOpen && (
                    <ul className="divide-y divide-border border-t border-border" data-testid="older-needs-list">
                      {olderItems.map(renderRow)}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </section>
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
