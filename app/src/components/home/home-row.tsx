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
  ListChecks,
  Lock,
  Mail,
  MessageCircle,
  MessageSquare,
  MessageSquareWarning,
  Truck,
  X,
} from "lucide-react";
import Link from "next/link";
import { useState, type ReactNode } from "react";
import { canonicalObjectHref } from "@/components/autopilot/alert";
import { noteParts } from "@/components/autopilot/lines";
import { PreparedCard } from "@/components/autopilot/prepared";
import { CloseControl } from "@/components/late/close-control";
import { Button } from "@/components/ui/button";
import { isObjectType, setHomeItemState, type PreparedItem } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatMoney, objectHref, readable, safeHref } from "@/lib/format";
import type { HomeNeedItem } from "@/lib/types";
import {
  cleanNeedCopy,
  isCommitmentItem,
  isHoldItem,
  itemBadgeOf,
  KIND_WORDS,
  ONLY_YOU_RE,
  sourceLabelOf,
  timingMeta,
} from "./home-copy";
import type { HomeDensity, HomeLayout } from "./home-prefs";

/** Resolve a canonical, always-loadable app route for a Home item. */
export function resolveNeedHref(item: HomeNeedItem): string | null {
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

export function KindIcon({ item, className }: { item: HomeNeedItem; className?: string }) {
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
      <PreparedCard
        item={prepared}
        onGone={() => onGone(item.id, `Updated ${readable(item.title, { shortDates: true })}`)}
        onChanged={() => undefined}
      />
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

function askHrefOf(item: HomeNeedItem, titleText: string): string {
  return item.object_id
    ? `/ask?q=${encodeURIComponent(`What is the latest on ${titleText}?`)}&object=${encodeURIComponent(item.object_id)}`
    : `/ask?q=${encodeURIComponent(`What is the latest on ${titleText}?`)}`;
}

export function NeedInlineDetail({
  item,
  resolvedHref,
  onClose,
  onGone,
  onQuickState,
}: {
  item: HomeNeedItem;
  resolvedHref: string | null;
  onClose: () => void;
  onGone: (id: string, note?: string) => void;
  onQuickState?: (item: HomeNeedItem, nextState: "closed" | "snoozed", title: string) => void;
}) {
  const srcLabel = sourceLabelOf(item);
  const copy = cleanNeedCopy(item);
  const parts = noteParts(item.line ?? "");
  const timing = timingMeta(item, copy.extractedDue);
  const badge = itemBadgeOf(item);
  const nextStep = !item.matter_id && item.ask ? readable(item.ask, { shortDates: true }).trim() : "";
  const askHref = askHrefOf(item, copy.title);
  const [acting, setActing] = useState<"snoozed" | "closed" | null>(null);
  const [actError, setActError] = useState<string | null>(null);

  const triggerState = async (nextState: "closed" | "snoozed") => {
    if (onQuickState) {
      onQuickState(item, nextState, copy.title);
      return;
    }
    if (acting) return;
    setActing(nextState);
    setActError(null);
    const until = nextState === "snoozed" ? new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() : undefined;
    try {
      await setHomeItemState(item.id, nextState, until);
      onGone(item.id, nextState === "snoozed" ? `Snoozed ${copy.title} for 1 day` : `Closed ${copy.title}`);
    } catch (e) {
      setActError(plainError(e));
    } finally {
      setActing(null);
    }
  };

  return (
    <div
      className="ml-6 mb-2 border-l-2 border-border-strong bg-bg-subtle/60 py-3 pr-3 pl-4 sm:ml-8"
      data-testid="need-detail"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-2.5">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted">
            <span className="font-medium text-fg">{KIND_WORDS[item.kind] ?? item.kind}</span>
            <span>{srcLabel}</span>
            {badge && (
              <span
                data-testid="need-detail-badge"
                className={cn(
                  "font-medium",
                  badge === "Verify unit cost" ? "text-risk" : "text-fg/85",
                )}
              >
                {badge}
              </span>
            )}
            {timing.badge && (
              <span className={cn("num font-medium", timing.overdue ? "text-late" : "text-muted")}>
                {timing.badge}
                {timing.overdue && timing.dueShort ? ` (Due ${timing.dueShort})` : ""}
              </span>
            )}
            {copy.person && <span className="text-fg/85">{copy.person}</span>}
            {item.amount && (
              <span className="num font-medium text-fg">
                {formatMoney(item.amount.amount, item.amount.currency)}
              </span>
            )}
            {copy.waitingLabel ? (
              <span>{copy.waitingLabel}</span>
            ) : copy.actionLabel ? (
              <span>{copy.actionLabel}</span>
            ) : null}
          </div>

          <h2 className="text-[15px] font-semibold leading-5 tracking-[-0.015em] text-fg">{copy.title}</h2>

          {copy.subtitle && (
            <p className="text-[13.5px] leading-5 text-fg/90">{copy.subtitle}</p>
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

          {nextStep && nextStep !== copy.subtitle && (
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
            <p className="border-l-2 border-l-risk/70 py-0.5 pl-3 text-[12px] text-muted">
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
            <Button
              type="button"
              variant="ghost"
              size="xs"
              loading={acting === "closed"}
              disabled={acting !== null}
              onClick={() => void triggerState("closed")}
              aria-label="Close item"
              data-testid="need-dismiss"
            >
              <Check className="size-3.5" strokeWidth={2.2} aria-hidden />
              {acting === "closed" ? "Closing" : "Done"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              loading={acting === "snoozed"}
              disabled={acting !== null}
              onClick={() => void triggerState("snoozed")}
              aria-label="Snooze 1 day"
              data-testid="need-snooze"
            >
              <Clock3 className="size-3.5" aria-hidden />
              {acting === "snoozed" ? "Snoozing" : "Snooze 1 day"}
            </Button>
          </div>
          {actError && (
            <p role="alert" className="text-[12px] text-late" data-testid="need-act-error">
              {actError}
            </p>
          )}

          {item.kind === "late_po" && item.object_id && (
            <div className="border-t border-border pt-2.5">
              <CloseControl order={{ object_id: item.object_id }} onChanged={() => onGone(item.id, `Closed ${copy.title}`)} />
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

export function NeedRow({
  item,
  mergedCount = 0,
  density = "comfortable",
  layout = "grouped",
  on,
  onPick,
  onClose,
  onGone,
  onQuickState,
}: {
  item: HomeNeedItem;
  mergedCount?: number;
  density?: HomeDensity;
  layout?: HomeLayout;
  on: boolean;
  onPick: () => void;
  onClose: () => void;
  onGone: (id: string, note?: string) => void;
  onQuickState?: (item: HomeNeedItem, nextState: "closed" | "snoozed", title: string) => void;
}) {
  const resolvedHref = resolveNeedHref(item);
  const href = resolvedHref ?? "/";
  const srcLabel = sourceLabelOf(item);
  const copy = cleanNeedCopy(item);
  const timing = timingMeta(item, copy.extractedDue);
  const badge = itemBadgeOf(item);
  const hold = isHoldItem(item);
  const commitment = isCommitmentItem(item);
  const waiting = item.kind === "waiting" || !!copy.waitingLabel;
  const askHref = askHrefOf(item, copy.title);

  const [acting, setActing] = useState<"snoozed" | "closed" | null>(null);
  const [rowErr, setRowErr] = useState<string | null>(null);

  const runRowState = async (nextState: "closed" | "snoozed") => {
    if (onQuickState) {
      onQuickState(item, nextState, copy.title);
      return;
    }
    if (acting) return;
    setActing(nextState);
    setRowErr(null);
    const until = nextState === "snoozed" ? new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() : undefined;
    try {
      await setHomeItemState(item.id, nextState, until);
      onGone(item.id, nextState === "snoozed" ? `Snoozed ${copy.title} for 1 day` : `Closed ${copy.title}`);
    } catch (e) {
      setRowErr(plainError(e));
    } finally {
      setActing(null);
    }
  };

  // Crisp, calm 1-line context without middot soup or redundant verbs.
  const leadLabel = hold
    ? [copy.holdImpact, copy.person].filter(Boolean).join(", ") || null
    : waiting
      ? copy.waitingLabel || copy.person || null
      : commitment
        ? copy.person || "Commitment"
        : copy.person || null;
  const secondaryDue =
    hold && timing.dueShort
      ? `Due ${timing.dueShort}`
      : waiting && timing.overdue && timing.dueShort
        ? `Due ${timing.dueShort}`
        : commitment && timing.dueShort
          ? `Due ${timing.dueShort}`
          : timing.overdue && timing.dueShort
            ? `Due ${timing.dueShort}`
            : null;
  const summaryText = copy.subtitle || (!leadLabel && copy.actionLabel ? copy.actionLabel : "");

  // Avoid echoing the same date/age in both metadata and right timing slot.
  const showRightTiming =
    !!timing.badge &&
    !(commitment && !timing.overdue && timing.dueShort) &&
    !(waiting && !timing.overdue && timing.ageShort);

  const compact = density === "compact";

  return (
    <li className="cv-auto">
      <div
        className={cn(
          "group relative flex items-center gap-2.5 px-2 transition-colors sm:px-2.5",
          compact ? "py-2" : "py-2.5",
          on
            ? "bg-row-on before:absolute before:inset-y-1.5 before:left-0 before:w-[2px] before:bg-accent"
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
            <KindIcon item={item} className={on ? "text-fg" : "text-faint group-hover:text-muted"} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 items-baseline justify-between gap-x-4">
              <span
                className={cn(
                  "min-w-0 flex-1 truncate text-[14px] font-medium leading-5 tracking-[-0.01em] text-fg",
                  layout === "split" && "line-clamp-2 whitespace-normal",
                )}
                title={copy.title}
                data-testid="need-title"
              >
                {copy.title}
                {mergedCount > 0 && (
                  <span
                    data-testid="need-merged-count"
                    className="num ml-1.5 text-[12px] font-normal text-faint"
                    title={`${mergedCount} duplicate ${mergedCount === 1 ? "update" : "updates"} collapsed`}
                  >
                    (+{mergedCount} {mergedCount === 1 ? "update" : "updates"})
                  </span>
                )}
              </span>
              {(badge || item.draft_ready || srcLabel || showRightTiming) && (
                <span className="inline-flex shrink-0 items-baseline gap-2.5 text-[12px] leading-5 whitespace-nowrap">
                  {badge && (
                    <span
                      data-testid="need-badge"
                      className={cn(
                        "font-medium",
                        badge === "Verify unit cost" ? "text-risk" : "text-muted",
                      )}
                    >
                      {badge}
                    </span>
                  )}
                  {item.draft_ready && (
                    <span className="font-medium text-ok">
                      Draft ready
                    </span>
                  )}
                  {srcLabel && (
                    <span data-testid="need-source" className="text-faint">
                      {srcLabel}
                    </span>
                  )}
                  {showRightTiming && (
                    <span
                      className={cn(
                        "num",
                        timing.overdue ? "font-medium text-late" : "text-faint",
                      )}
                    >
                      {timing.badge}
                    </span>
                  )}
                </span>
              )}
            </span>

            {(leadLabel || secondaryDue || summaryText) && (
              <span
                className={cn(
                  "flex min-w-0 items-baseline gap-x-2.5 text-[12.5px] leading-[18px] text-muted",
                  compact ? "mt-0.5" : "mt-0.5",
                )}
              >
                {leadLabel && (
                  <span
                    title={leadLabel}
                    className="max-w-[210px] shrink-0 truncate font-medium text-fg/80"
                  >
                    {leadLabel}
                  </span>
                )}
                {secondaryDue && (
                  <span className="num shrink-0 text-[12px] text-faint">
                    {secondaryDue}
                  </span>
                )}
                {summaryText && (
                  <span title={summaryText} className="min-w-0 flex-1 truncate text-muted">
                    {summaryText}
                  </span>
                )}
              </span>
            )}

            {copy.otherLabels.length > 0 && (
              <span className="mt-0.5 flex flex-wrap gap-2 text-[11.5px] text-faint" data-testid="need-scope">
                {copy.otherLabels.map((label) => (
                  <span key={label}>{label.replace(/\.$/, "")}</span>
                ))}
              </span>
            )}
          </span>
        </a>

        {/* Universal 1-click Close + Snooze on every row: visible on hover/focus and always accessible on touch */}
        <div className="flex shrink-0 items-center gap-0.5" data-testid="row-quick-actions">
          {copy.isPrivate && (
            <span
              className="inline-flex size-6 shrink-0 items-center justify-center rounded-sm text-faint opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
              title="Only you can see this"
              aria-label="Only you can see this"
              data-testid="need-private"
            >
              <Lock className="size-3" strokeWidth={2} aria-hidden />
            </span>
          )}
          <div className="flex items-center gap-0.5 opacity-100 transition-opacity duration-150 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100 [@media(hover:none)]:opacity-100">
            <button
              type="button"
              disabled={acting !== null}
              onClick={() => void runRowState("closed")}
              aria-label="Close item"
              title="Close item"
              data-testid="row-quick-dismiss"
              className="inline-flex size-7 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-ok focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:opacity-50 cursor-pointer"
            >
              <Check className="size-3.5" strokeWidth={2.2} aria-hidden />
            </button>
            <button
              type="button"
              disabled={acting !== null}
              onClick={() => void runRowState("snoozed")}
              aria-label="Snooze 1 day"
              title="Snooze 1 day"
              data-testid="row-quick-snooze"
              className="inline-flex size-7 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:opacity-50 cursor-pointer"
            >
              <Clock3 className="size-3.5" strokeWidth={2} aria-hidden />
            </button>
            {resolvedHref && resolvedHref !== "/" && (
              <Link
                href={resolvedHref}
                aria-label={`Open ${copy.title}`}
                title="Open"
                data-testid="row-quick-open"
                className="hidden size-7 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 sm:inline-flex"
              >
                <ArrowUpRight className="size-3.5" strokeWidth={2} aria-hidden />
              </Link>
            )}
            <Link
              href={askHref}
              aria-label={`Ask about ${copy.title}`}
              title="Ask"
              data-testid="row-quick-ask"
              className="hidden size-7 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 sm:inline-flex"
            >
              <MessageCircle className="size-3.5" strokeWidth={2} aria-hidden />
            </Link>
          </div>
          <span className="inline-flex size-5 items-center justify-center text-faint transition-colors group-hover:text-muted" aria-hidden>
            {on ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          </span>
        </div>
      </div>

      {rowErr && (
        <p role="alert" className="px-4 pb-2 text-[12px] text-late" data-testid="row-quick-error">
          {rowErr}
        </p>
      )}

      {on && (
        <NeedInlineDetail
          item={item}
          resolvedHref={resolvedHref}
          onClose={onClose}
          onGone={onGone}
          onQuickState={onQuickState}
        />
      )}
    </li>
  );
}

/** Open Linear-style collapsible section container on the main canvas with a crisp hairline header. */
export function HomeSectionCard({
  title,
  description,
  count,
  icon,
  open,
  onToggle,
  headerAction,
  testId,
  toggleTestId,
  children,
}: {
  title: string;
  description?: string;
  count: number;
  dotClass?: string;
  icon?: ReactNode;
  open: boolean;
  onToggle: () => void;
  headerAction?: ReactNode;
  testId?: string;
  toggleTestId?: string;
  children: ReactNode;
}) {
  return (
    <section data-testid={testId} className="min-w-0">
      <div className="flex items-center gap-2 border-b border-border/80 px-1 py-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={onToggle}
          data-testid={toggleTestId}
          className="group/hdr flex min-w-0 flex-1 items-center gap-2 text-left transition-colors cursor-pointer"
        >
          {open ? (
            <ChevronDown className="size-3.5 shrink-0 text-faint transition-colors group-hover/hdr:text-fg" aria-hidden />
          ) : (
            <ChevronRight className="size-3.5 shrink-0 text-faint transition-colors group-hover/hdr:text-fg" aria-hidden />
          )}
          <h2 className="shrink-0 text-[13.5px] font-semibold tracking-[-0.01em] text-fg">{title}</h2>
          <span className="num shrink-0 text-[12px] font-medium text-faint">{count}</span>
          {description && (
            <span className="hidden truncate text-[12.5px] font-normal text-faint sm:inline">
              {description}
            </span>
          )}
        </button>
        {headerAction && (
          <div className="flex shrink-0 items-center gap-1.5 pl-1">
            {headerAction}
          </div>
        )}
      </div>
      {open && <div className="pt-1">{children}</div>}
    </section>
  );
}
