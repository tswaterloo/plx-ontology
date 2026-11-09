"use client";

import { ThumbsDown, ThumbsUp } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useFeedbackDialog } from "@/components/feedback-dialog";
import { LoadProblem } from "@/components/ui/notice";
import { Toolbar } from "@/components/ui/toolbar";
import { useAuth } from "@/components/auth";
import { getFeedback, setFeedbackStatus } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { useListKeys } from "@/lib/list-keys";
import type { FeedbackEntry, FeedbackKind, FeedbackList, FeedbackStatus } from "@/lib/types";

const KIND_WORDS: Record<FeedbackKind, string> = {
  answer: "Answer",
  alert: "Alert",
  page: "Page",
  record: "Record",
  list: "List",
  ui: "The app",
  general: "General",
};

const STATUS_WORDS: Record<FeedbackStatus, string> = { new: "New", accepted: "Accepted", fixed: "Fixed", wont_fix: "Won't fix" };
const STATUSES = Object.keys(STATUS_WORDS) as FeedbackStatus[];
const VERDICT_WORDS: Record<string, string> = { useful: "Useful", not_useful: "Not useful", wrong: "Wrong" };

/** One line saying what the feedback is: the rating or verdict, then its subject. */
function headline(e: FeedbackEntry): string {
  if (e.kind === "answer") return e.question || "An answer";
  if (e.kind === "alert") return `${VERDICT_WORDS[e.verdict ?? ""] ?? "Marked"}: ${e.label || e.item_id || "an alert"}`;
  return e.note || KIND_WORDS[e.kind];
}

function Rating({ e }: { e: FeedbackEntry }) {
  if (e.kind === "answer" && e.rating) {
    const Icon = e.rating > 0 ? ThumbsUp : ThumbsDown;
    return <Icon className={cn("size-[15px]", e.rating > 0 ? "text-muted" : "text-fg")} strokeWidth={2} aria-label={e.rating > 0 ? "Thumbs up" : "Thumbs down"} />;
  }
  return <span className="size-[15px]" aria-hidden />;
}

function Row({ e, on, onPick, admin, children }: { e: FeedbackEntry; on: boolean; onPick: () => void; admin: boolean; children?: React.ReactNode }) {
  const sub = [KIND_WORDS[e.kind], e.kind === "answer" || e.kind === "alert" ? e.note : e.page, admin && !e.mine ? e.reader_name : null].filter(Boolean).join(" · ");
  return (
    <li>
      <a
        href={`#${e.id}`}
        data-row-link
        aria-current={on ? "true" : undefined}
        onClick={(ev) => {
          ev.preventDefault();
          onPick();
        }}
        onFocus={onPick}
        className={cn(
          "grid grid-cols-[18px_minmax(0,1fr)_auto] items-start gap-x-2.5 rounded-[10px] px-3 py-2.5 outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
          on ? "bg-[color-mix(in_srgb,var(--fg)_7%,transparent)]" : "hover:bg-hover",
        )}
        data-testid="feedback-row"
      >
        <span className="pt-px">
          <Rating e={e} />
        </span>
        <span className="min-w-0">
          <span className="block truncate text-[13px] leading-[18px] font-medium first-letter:uppercase">{headline(e)}</span>
          <span className="mt-0.5 block truncate text-[12px] leading-4 text-muted">{sub}</span>
        </span>
        <span className="text-right text-[12px] leading-[18px] tabular-nums text-muted">
          <span className="block">{formatDateTime(e.at)}</span>
          <span className={cn("block", e.status === "new" ? "font-medium text-fg" : "text-faint")}>{STATUS_WORDS[e.status]}</span>
        </span>
      </a>
      {/* a phone has no detail pane: the picked entry opens under its row */}
      {on && children && <div className="lg:hidden">{children}</div>}
    </li>
  );
}

/** An app path as words: its query decoded ("/ask?q=where is PO 90412"). */
function readablePath(path: string): string {
  try {
    return decodeURIComponent(path.replace(/\+/g, " "));
  } catch {
    return path;
  }
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="mb-1 text-[11px] font-medium leading-4 text-faint">{label}</dt>
      <dd className="text-[13px] leading-5 whitespace-pre-wrap">{children}</dd>
    </div>
  );
}

function Detail({ e, admin, onStatus }: { e: FeedbackEntry; admin: boolean; onStatus: (s: FeedbackStatus) => void }) {
  const flagged = new Set(e.flagged ?? []);
  const record = e.object_type && e.object_id && e.object_type !== "Source" ? `/object/${encodeURIComponent(e.object_type)}/${encodeURIComponent(e.object_id)}` : null;
  const page = e.page && e.page.startsWith("/") && !e.page.startsWith("//") ? e.page : null;
  return (
    <article className="space-y-5 px-5 py-5" data-testid="feedback-detail" aria-label="Feedback detail">
      <header className="space-y-1">
        <p className="text-[12px] text-muted">
          {KIND_WORDS[e.kind]}
          {e.reader_name ? ` · ${e.mine ? "You" : e.reader_name}` : ""} · {formatDateTime(e.at)}
        </p>
        <h2 className="text-[15px] leading-5 font-semibold tracking-[-0.01em] first-letter:uppercase">{headline(e)}</h2>
      </header>
      {admin ? (
        <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="Status" data-testid="feedback-status">
          {STATUSES.map((s) => (
            <button
              key={s}
              type="button"
              role="radio"
              aria-checked={e.status === s}
              onClick={() => e.status !== s && onStatus(s)}
              className={cn(
                "inline-flex h-7 items-center rounded-md px-2.5 text-[12px] cursor-pointer",
                e.status === s ? "bg-accent-soft font-medium text-fg" : "bg-surface-2 text-fg hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]",
              )}
            >
              {STATUS_WORDS[s]}
            </button>
          ))}
        </div>
      ) : (
        <p className="text-[12px] text-muted">Status: {STATUS_WORDS[e.status]}</p>
      )}
      <dl className="space-y-4">
        {e.note && <Field label={e.kind === "answer" ? "What was wrong" : "Note"}>{e.note}</Field>}
        {e.expected && <Field label="The right answer is">{e.expected}</Field>}
        {e.answer && <Field label="The answer given">{e.answer}</Field>}
        {(e.claims ?? []).length > 0 && (
          <Field label="Claims">
            <ul className="space-y-1.5">
              {(e.claims ?? []).map((c) => (
                <li key={c.i} className={cn("text-[12px] leading-[18px]", flagged.has(c.i) ? "text-fg" : "text-muted")}>
                  {flagged.has(c.i) && <span className="mr-1.5 font-medium">Marked wrong:</span>}
                  {c.text}
                  {(c.citations ?? []).length > 0 && <span className="text-faint"> · {(c.citations ?? []).map((s) => s.label || s.system).join(", ")}</span>}
                </li>
              ))}
            </ul>
          </Field>
        )}
        {(page || record) && (
          <Field label="Where">
            <span className="flex flex-wrap gap-x-3">
              {page && (
                <Link href={page} className="font-medium break-all hover:underline">
                  {readablePath(page)}
                </Link>
              )}
              {record && (
                <Link href={record} className="font-medium hover:underline">
                  {e.label || `${e.object_type} ${e.object_id}`}
                </Link>
              )}
            </span>
          </Field>
        )}
        {(e.withheld ?? []).length > 0 && (
          <Field label="Withheld">
            <span className="block text-[12px] leading-[18px] text-muted" data-testid="feedback-withheld">
              Parts of this entry are not shown: they carry {(e.withheld ?? []).map((w) => w.replace("_", " ")).join(", ")}, which your access does not include.
            </span>
          </Field>
        )}
        <p className="text-[11px] leading-4 text-faint">
          {[e.app_version && `App ${e.app_version}`, e.served_version && `data ${e.served_version}`].filter(Boolean).join(" · ")}
        </p>
      </dl>
    </article>
  );
}

const SELECT = "h-7 rounded-md bg-surface-2 px-2 text-[12px] text-fg focus:outline-2 focus:outline-ring cursor-pointer";

/** Feedback: yours, or every reader's for the owner and the admins, to triage. */
export default function FeedbackPage() {
  const { state } = useAuth();
  const dialog = useFeedbackDialog();
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [order, setOrder] = useState<"newest" | "oldest">("newest");
  const [page, setPage] = useState("");
  const [list, setList] = useState<FeedbackList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  useListKeys(listRef);

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getFeedback({ kind, status, order, page: page.trim() || undefined, limit: 100 }, c.signal)
      .then((l) => setList(l))
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, [kind, status, order, page]);
  useEffect(() => load(), [load]);

  const entries = useMemo(() => list?.entries ?? [], [list]);
  const current = entries.find((e) => e.id === picked) ?? entries[0] ?? null;
  const admin = !!list?.all_readers || (state.status === "signed-in" && !!state.me.feedback_admin);
  const allCount = list ? Object.values(list.counts).reduce((a, b) => a + b, 0) : 0;

  async function changeStatus(id: string, s: FeedbackStatus) {
    setSaveError(null);
    try {
      const done = await setFeedbackStatus(id, s);
      // shown at once; the counts (and a status filter) follow from the server
      setList((l) => l && { ...l, entries: l.entries.map((x) => (x.id === id ? { ...x, status: done.status } : x)) });
      load();
    } catch (e) {
      setSaveError(plainError(e));
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar
        title={
          <>
            {admin ? "Feedback" : "Your feedback"}
            {list && <span className="ml-2 text-[13px] font-normal tracking-normal tabular-nums text-faint">{list.total}</span>}
          </>
        }
        className="bg-[color-mix(in_srgb,var(--list)_90%,transparent)]"
        actions={
          <button type="button" onClick={dialog.open} className="inline-flex h-8 items-center rounded-lg px-2.5 text-[13px] font-medium hover:bg-hover cursor-pointer">
            Give feedback
          </button>
        }
      />
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-4 py-2 lg:px-5" data-testid="feedback-filters">
        <div className="mr-2 flex gap-0.5" role="tablist" aria-label="Status">
          {[["", "All", allCount] as const, ...STATUSES.map((s) => [s, STATUS_WORDS[s], list?.counts[s] ?? 0] as const)].map(([value, label, n]) => (
            <button
              key={value || "all"}
              type="button"
              role="tab"
              aria-selected={status === value}
              onClick={() => setStatus(value)}
              className={cn("inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] cursor-pointer", status === value ? "bg-[color-mix(in_srgb,var(--fg)_7%,transparent)] font-medium" : "text-muted hover:bg-hover hover:text-fg")}
            >
              {label}
              {list && <span className="tabular-nums text-faint">{n}</span>}
            </button>
          ))}
        </div>
        <label className="sr-only" htmlFor="fb-kind">
          Type
        </label>
        <select id="fb-kind" value={kind} onChange={(e) => setKind(e.target.value)} className={SELECT}>
          <option value="">Every type</option>
          {(Object.keys(KIND_WORDS) as FeedbackKind[]).map((k) => (
            <option key={k} value={k}>
              {KIND_WORDS[k]}
            </option>
          ))}
        </select>
        <label className="sr-only" htmlFor="fb-page">
          Page
        </label>
        <input id="fb-page" value={page} onChange={(e) => setPage(e.target.value)} placeholder="Page, e.g. /late" className={cn(SELECT, "w-36 cursor-text placeholder:text-faint")} />
        <label className="sr-only" htmlFor="fb-order">
          Order
        </label>
        <select id="fb-order" value={order} onChange={(e) => setOrder(e.target.value as "newest" | "oldest")} className={SELECT}>
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
      </div>
      {error ? (
        <div className="p-5">
          <LoadProblem message={error} onRetry={load} />
        </div>
      ) : !list ? (
        <ul className="space-y-1 px-2 pt-3 sm:px-3" aria-busy="true" aria-label="Loading feedback">
          {Array.from({ length: 6 }).map((_, i) => (
            <li key={i} className="flex items-center gap-3 px-3 py-2.5">
              <span className="flex-1 space-y-2">
                <span className={cn("skeleton block h-3", ["w-1/2", "w-2/5", "w-1/3"][i % 3])} />
                <span className="skeleton block h-2.5 w-1/4" />
              </span>
            </li>
          ))}
        </ul>
      ) : entries.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center" data-testid="feedback-empty">
          <p className="text-[13px] font-medium">No feedback.</p>
          <p className="mt-1 max-w-[320px] text-[12px] leading-[18px] text-muted">Press F on any page to send a note.</p>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1">
          <ul ref={listRef} className="min-w-0 flex-1 space-y-px overflow-y-auto px-2 py-2 sm:px-3 lg:max-w-[460px] lg:border-r lg:border-border" data-testid="feedback-list">
            {entries.map((e) => (
              <Row key={e.id} e={e} on={current?.id === e.id} onPick={() => setPicked(e.id)} admin={admin}>
                {picked === e.id && <Detail e={e} admin={admin} onStatus={(s) => void changeStatus(e.id, s)} />}
              </Row>
            ))}
          </ul>
          <div className="hidden min-w-0 flex-1 overflow-y-auto bg-bg lg:block">
            {current && <Detail e={current} admin={admin} onStatus={(s) => void changeStatus(current.id, s)} />}
            {saveError && (
              <p role="alert" className="px-5 text-[12px] text-fg">
                {saveError}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
