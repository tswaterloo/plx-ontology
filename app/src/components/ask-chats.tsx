"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Ellipsis, EyeOff, Layers, Loader2, MessageSquare, PanelLeftClose, Pin, Plus, Search, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { getAskChats, updateAskChat } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { useShortcut } from "@/lib/platform";
import { useAskRunning } from "@/lib/thread";
import type { AskChat, AskChats } from "@/lib/types";

/** Rows drawn at first; more are drawn as the list is scrolled, so every chat is reachable and none is cut. */
const DRAW_STEP = 60;

const TITLE_MAX_CHARS = 36;
const SPACE_PREFIX = /^Focus\s+on\s+[^:]{1,140}:\s*/i;
const TRAILING_PARENS = /\s*\([^)]*\)?\s*$/;
const TRAILING_FILLER = /(?:\s+(?:right\s+now|this\s+today|today\s+please|for\s+me|please|asap|thanks|thank\s+you))+[.?!,;:]*\s*$/i;
const LEADING_FILLER = /^(?:hey\s+petra[,:]?\s*|hi[,:]?\s*|hello[,:]?\s*|no[,\s]+any\s+other\s+|no[,\s]+|any\s+other\s+|actually[,\s]+|wait[,\s]+|also[,\s]+|okay[,\s]+|ok[,\s]+|well[,\s]+|so[,\s]+|can\s+you\s+|could\s+you\s+|would\s+you\s+|please\s+|figure\s+out\s+|find\s+out\s+|show\s+me\s+|tell\s+me\s+|give\s+me\s+|help\s+me\s+|list\s+(?:all\s+|me\s+)?|plot\s+(?:me\s+|our\s+|the\s+)?|graph\s+(?:me\s+|our\s+|the\s+)?|chart\s+(?:me\s+|our\s+|the\s+)?|check\s+(?:on\s+|our\s+|my\s+)?|i\s+want\s+to\s+know\s+|i\s+need\s+to\s+know\s+|i\s+need\s+to\s+|remember\s+(?:that\s+)?(?:i\s+(?:want|prefer|like|always\s+want|need)\s+(?:to\s+)?)?|our\s+|my\s+|the\s+)/i;

const TODAY_PRIORITIES_RE = /^(?:what\s+(?:do\s+i|should\s+i|have\s+i\s+got)\s+(?:have\s+to\s+|need\s+to\s+)?do\b.*(?:today|this\s+morning|now)?|my\s+(?:tasks?|priorities|to[- ]?dos?)\s+(?:for\s+)?(?:this\s+)?today\b)/i;
const NEEDS_ATTENTION_RE = /^(?:is\s+there\s+)?anything\s+(?:that\s+)?needs?\s+(?:my\s+)?attention\b/i;
const CHART_OVERVIEW_RE = /^(?:chart\s+and\s+(?:give|show|tell)\s+me\s+(?:all\s+)?(?:info|information|details?)\s+(?:about|on|for)\s+|(?:give|show|tell)\s+me\s+(?:all\s+)?(?:info|information|details?|everything)\s+(?:about|on|for)\s+)(.+)$/i;
const PREPARE_DOC_RE = /^(?:prepare|create|write|draft|make|build)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:document|doc|report|brief|summary|memo)\s+(?:for|about|on)\s+(?:our\s+|my\s+|the\s+)?(.+)$/i;
const WHATS_GOING_ON_RE = /^(?:what'?s|whats|what\s+is)\s+(?:going\s+on|happening|up|the\s+status)\s+(?:with|on|for)\s+(?:our\s+|the\s+)?(.+)$/i;
const BAR_CHART_RE = /^(?:(?:bar|line|pie|area|stacked)\s+)?chart\s+(?:of|for|showing)\s+(?:our\s+|my\s+|the\s+)?(.+)$/i;
const INVOICE_CURRENCY_RE = /^what\s+did\s+we\s+invoice\s+in\s+(usd|cad|eur|gbp)\b/i;
const TOP_SUPPLIER_RE = /^which\s+suppliers?\s+(?:has|have)\s+the\s+most\b/i;
const WHICH_ARE_RE = /^which\s+(?:of\s+(?:our|my|the)\s+)?(.+?)\s+(?:are|is|were|have)\s+([a-z0-9\s-]{2,24})[?.\s]*$/i;
const WHICH_PREFIX_RE = /^which\s+(?:of\s+(?:our|my|the)\s+)?/i;

const DANGLING_TAIL_WORDS = new Set([
  "and", "or", "but", "so", "why", "how", "what", "when", "where", "which", "who",
  "the", "a", "an", "to", "of", "for", "in", "on", "at", "by", "with", "from",
  "about", "into", "as", "if", "because", "is", "are", "was", "were", "be", "been",
  "that", "this", "these", "those", "it", "my", "our", "your", "their", "create",
  "make", "right", "now", "please", "last", "next", "very", "most", "all",
]);

const MINOR_TITLE_WORDS = new Set([
  "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of",
  "on", "or", "per", "the", "to", "vs", "with",
]);

const ACRONYM_MAP: Record<string, string> = {
  usd: "USD",
  cad: "CAD",
  eur: "EUR",
  gbp: "GBP",
  po: "PO",
  pos: "POs",
  so: "SO",
  sos: "SOs",
  qc: "QC",
  qa: "QA",
  erp: "ERP",
  bc: "BC",
  ytd: "YTD",
  mtd: "MTD",
  qtd: "QTD",
  sku: "SKU",
  skus: "SKUs",
  coa: "COA",
  coas: "COAs",
  "3pl": "3PL",
  ap: "AP",
  ar: "AR",
  kpi: "KPI",
  kpis: "KPIs",
  bom: "BOM",
  eta: "ETA",
  etas: "ETAs",
  sql: "SQL",
  pdf: "PDF",
  xlsx: "XLSX",
  rfq: "RFQ",
  rma: "RMA",
  mc: "MC",
  q1: "Q1",
  q2: "Q2",
  q3: "Q3",
  q4: "Q4",
};

function formatTitleWord(word: string, index: number): string {
  if (!word) return word;
  const m = /^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9]*)$/.exec(word);
  if (!m) return word;
  const [, lead, core, trail] = m;
  if (!core) return word;
  const low = core.toLowerCase();
  if (ACRONYM_MAP[low]) return `${lead}${ACRONYM_MAP[low]}${trail}`;
  if (/\d/.test(core) && /[A-Za-z]/.test(core)) return `${lead}${core.toUpperCase()}${trail}`;
  if (core.length >= 2 && core === core.toUpperCase() && /[A-Z]/.test(core)) return `${lead}${core}${trail}`;
  if (index > 0 && MINOR_TITLE_WORDS.has(low)) return `${lead}${low}${trail}`;
  if (core.includes("-")) {
    const parts = core.split("-").map((p) => {
      const pl = p.toLowerCase();
      if (ACRONYM_MAP[pl]) return ACRONYM_MAP[pl];
      return p ? p[0].toUpperCase() + p.slice(1).toLowerCase() : "";
    });
    return `${lead}${parts.join("-")}${trail}`;
  }
  return `${lead}${core[0].toUpperCase()}${core.slice(1)}${trail}`;
}

function toTitleCase(text: string): string {
  return text
    .split(" ")
    .filter(Boolean)
    .map((w, i) => formatTitleWord(w, i))
    .join(" ");
}

function trimDanglingTail(text: string, minWords = 2): string {
  const words = text.split(" ").filter(Boolean);
  while (words.length > minWords) {
    const tail = words[words.length - 1].toLowerCase().replace(/[^a-z0-9]+$/g, "");
    if (!tail || DANGLING_TAIL_WORDS.has(tail)) words.pop();
    else break;
  }
  return words.join(" ").replace(/[,;:\-?!.]+$/, "").trim();
}

function capOnWordBoundary(text: string, maxChars = TITLE_MAX_CHARS): string {
  const cleaned = text.trim().replace(/[,;:\-]+$/, "").trim();
  if (cleaned.length <= maxChars) {
    return trimDanglingTail(cleaned, 2) || cleaned;
  }
  const window = cleaned.slice(0, maxChars);
  const cutAt = window.lastIndexOf(" ");
  const clipped = (cutAt >= 12 ? window.slice(0, cutAt) : window).replace(/[,;:\-?!.]+$/, "").trim();
  return trimDanglingTail(clipped, 2) || clipped;
}

/**
 * Client-side Claude/Genie-style title cleaner (mirrored 1:1 in `operator_api/chat_titles.py`):
 * turns raw conversational prompts into crisp 2–5 word topic titles capped at ~36 chars on word boundaries.
 */
export function cleanChatTitle(raw: string, named = false): string {
  const collapsed = raw.trim().replace(/\s+/g, " ");
  if (!collapsed) return "Untitled Chat";
  if (named) return collapsed;

  let cleaned = collapsed.replace(SPACE_PREFIX, "").trim();

  // Keep only the crisp opening sentence when the user typed follow-up clauses.
  const firstBreak = cleaned.search(/[?!.]\s+/);
  if (firstBreak >= 10) {
    cleaned = cleaned.slice(0, firstBreak).trim();
  }

  cleaned = cleaned.replace(TRAILING_PARENS, "").trim();
  cleaned = cleaned.replace(/\.{2,}$/, "").trim();
  cleaned = cleaned.replace(/[?!.]+$/, "").trim();
  cleaned = cleaned.replace(TRAILING_FILLER, "").trim();

  // 1. High-signal conversational patterns mapped cleanly to crisp topic titles
  if (TODAY_PRIORITIES_RE.test(cleaned)) return "Today's Priorities";
  if (NEEDS_ATTENTION_RE.test(cleaned)) return "Items Needing Attention";

  const overviewMatch = CHART_OVERVIEW_RE.exec(cleaned);
  if (overviewMatch) {
    const subj = capOnWordBoundary(toTitleCase(trimDanglingTail(overviewMatch[1], 1)), TITLE_MAX_CHARS - 9);
    if (subj) return `${subj} Overview`;
  }

  const docMatch = PREPARE_DOC_RE.exec(cleaned);
  if (docMatch) {
    const subj = capOnWordBoundary(toTitleCase(trimDanglingTail(docMatch[1], 1)), TITLE_MAX_CHARS - 9);
    if (subj) return `${subj} Document`;
  }

  const statusMatch = WHATS_GOING_ON_RE.exec(cleaned);
  if (statusMatch) {
    const subj = capOnWordBoundary(
      toTitleCase(trimDanglingTail(statusMatch[1].replace(TRAILING_FILLER, "").trim(), 1)),
      TITLE_MAX_CHARS - 7,
    );
    if (subj) return `${subj} Status`;
  }

  const chartMatch = BAR_CHART_RE.exec(cleaned);
  if (chartMatch) {
    const core = chartMatch[1]
      .replace(TRAILING_FILLER, "")
      .split(/\b(?:for\s+the|over\s+the|in\s+the)\b/i)[0]
      .trim();
    const subj = capOnWordBoundary(toTitleCase(trimDanglingTail(core, 1)), TITLE_MAX_CHARS - 6);
    if (subj) return `${subj} Chart`;
  }

  const invMatch = INVOICE_CURRENCY_RE.exec(cleaned);
  if (invMatch) return `${invMatch[1].toUpperCase()} Invoicing`;

  if (TOP_SUPPLIER_RE.test(cleaned)) return "Top Supplier Comparison";

  const whichAreMatch = WHICH_ARE_RE.exec(cleaned);
  if (whichAreMatch) {
    const noun = whichAreMatch[1].trim();
    const adj = whichAreMatch[2].trim();
    if (
      noun.split(/\s+/).length <= 2 &&
      adj.split(/\s+/).length === 1 &&
      /^(?:late|overdue|open|blocked|missing)$/i.test(adj)
    ) {
      cleaned = `${adj} ${noun}`;
    } else {
      cleaned = noun;
    }
  } else if (WHICH_PREFIX_RE.test(cleaned)) {
    cleaned = cleaned.replace(WHICH_PREFIX_RE, "").replace(/\s+(?:are|is|were)\s*$/i, "").trim();
  }

  // 2. Strip leading conversational filler
  for (let i = 0; i < 5; i++) {
    const next = cleaned.replace(LEADING_FILLER, "").trim();
    if (!next || next === cleaned) break;
    cleaned = next;
  }

  // Re-strip trailing noise & normalize verbose ERP phrases when tight on space
  cleaned = cleaned
    .replace(TRAILING_PARENS, "")
    .replace(TRAILING_FILLER, "")
    .replace(/\s+for\s+(20\d{2})\b/gi, " $1")
    .replace(/[,;:\-?!.]+$/, "")
    .trim();

  if (cleaned.length > 22) {
    cleaned = cleaned
      .replace(/\bpurchase\s+orders\b/gi, "POs")
      .replace(/\bpurchase\s+order\b/gi, "PO")
      .replace(/\bquality\s+control\b/gi, "QC");
  }

  cleaned = trimDanglingTail(cleaned, 2);
  cleaned = capOnWordBoundary(cleaned, TITLE_MAX_CHARS);

  const finalTitle = toTitleCase(cleaned || collapsed.slice(0, TITLE_MAX_CHARS));
  return finalTitle || "Untitled Chat";
}

/** Crisp compact age label for sidebar rows ("now", "12m", "2h", "3d", "1w", "2mo"). */
export function compactAge(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const s = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
  if (s < 60) return "now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h`;
  const days = Math.round(h / 24);
  if (days < 7) return `${days}d`;
  if (days < 35) return `${Math.max(1, Math.round(days / 7))}w`;
  return `${Math.max(1, Math.round(days / 30))}mo`;
}

type Entry = { head: AskChat; same: AskChat[] };
const said = (c: AskChat) => cleanChatTitle(c.title, c.named).toLowerCase();

/** One entry per distinct title, newest first; the older chats with the same title ride behind it, still reachable. A chat you named is its own entry. */
function collapse(chats: AskChat[]): Entry[] {
  const out: Entry[] = [];
  const seen = new Map<string, Entry>();
  for (const c of chats) {
    const key = c.named ? `n:${c.conversation_id}` : said(c);
    const hit = seen.get(key);
    if (hit) hit.same.push(c);
    else {
      const e = { head: c, same: [] };
      seen.set(key, e);
      out.push(e);
    }
  }
  return out;
}

export const GROUPS = ["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"] as const;
/** Which time heading a chat sits under, by the calendar days between now and its last question. */
export function groupOf(last: string, now: Date): (typeof GROUPS)[number] {
  const t = new Date(last);
  if (Number.isNaN(t.getTime())) return "Older";
  const day = (d: Date) => Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((day(now) - day(t)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 7) return "Previous 7 days";
  if (days <= 30) return "Previous 30 days";
  return "Older";
}

function newestFirst(a: AskChat, b: AskChat): number {
  const ta = new Date(a.last_at).getTime();
  const tb = new Date(b.last_at).getTime();
  const va = Number.isNaN(ta) ? 0 : ta;
  const vb = Number.isNaN(tb) ? 0 : tb;
  return vb - va;
}

function ChatListSkeleton() {
  const widths = ["w-4/5", "w-3/5", "w-11/12", "w-2/3", "w-3/4"];
  return (
    <div className="space-y-[18px] pt-1" aria-busy="true" aria-label="Loading chats" data-testid="ask-chats-skeleton">
      <div>
        <div className="mx-2.5 mb-2 h-3 w-12 rounded bg-surface-2/90 skeleton-shimmer" />
        <div className="space-y-1">
          {widths.map((w, i) => (
            <div key={i} className="flex h-8 items-center rounded-lg px-2.5">
              <div className={cn("h-3.5 rounded bg-surface-2/80 skeleton-shimmer", w)} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * Your Ask conversations, styled as a calm desktop rail: full-width New chat and quiet borderless
 * search on top, then Pinned, Today, Yesterday, Previous 7 days, and Older groups with crisp
 * relative timestamps in the right column and a Liquid Glass hover action pill that fades cleanly
 * without ever colliding with title text.
 */
export function AskChatList({
  current,
  refreshKey,
  onNew,
  onCollapse,
  className,
}: {
  current?: string | null;
  refreshKey?: unknown;
  onNew: () => void;
  onCollapse?: () => void;
  className?: string;
}) {
  const [q, setQ] = useState("");
  const [list, setList] = useState<AskChats | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [drawn, setDrawn] = useState(DRAW_STEP);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const more = useRef<HTMLDivElement>(null);
  const { mod } = useShortcut();
  const newChatShortcut = mod === "⌘" ? "⌘⇧O" : "Ctrl+Shift+O";

  useEffect(() => {
    const c = new AbortController();
    // a search waits for the typing to pause; the first read and a refresh go at once
    const t = setTimeout(
      () =>
        getAskChats(q, c.signal).then(
          (got) => {
            setList(got);
            setProblem(null);
          },
          (e) => {
            if (!c.signal.aborted) setProblem(plainError(e));
          },
        ),
      q ? 250 : 0,
    );
    return () => {
      clearTimeout(t);
      c.abort();
    };
  }, [q, refreshKey]);

  const change = useCallback(
    (chat: AskChat, body: { name?: string; pinned?: boolean; deleted?: boolean }) => {
      updateAskChat(chat.conversation_id, body).then(
        (got) => {
          setProblem(null);
          // the server answers with the whole list; a search in progress reads its own again
          if (q) getAskChats(q).then(setList, () => undefined);
          else setList(got);
          if (body.deleted && chat.conversation_id === current) onNew();
        },
        (e) => setProblem(plainError(e)),
      );
    },
    [q, current, onNew],
  );

  const { runningIds, runningChats } = useAskRunning();
  const baseChats = list?.chats ?? [];
  const mergedChats: AskChat[] = (() => {
    const out = baseChats.map((c) => (runningIds.has(c.conversation_id) ? { ...c, running: true } : c));
    if (q.trim()) return out;
    const seenIds = new Set(out.map((c) => c.conversation_id));
    const extras: AskChat[] = [];
    for (const rc of runningChats) {
      if (seenIds.has(rc.conversation_id)) continue;
      extras.push({
        conversation_id: rc.conversation_id,
        title: rc.title,
        named: false,
        pinned: false,
        questions: 1,
        last_at: rc.started_at,
        running: true,
      });
    }
    return extras.length ? [...extras, ...out] : out;
  })();
  const chats = mergedChats;
  const pinned = chats.filter((c) => c.pinned).sort(newestFirst);
  const entries = collapse(chats.filter((c) => !c.pinned).sort(newestFirst));
  // the rows still to draw: as the foot of the list scrolls into view, the next batch is drawn
  const total = entries.reduce((n, e) => n + 1 + (open.has(e.head.conversation_id) ? e.same.length : 0), 0);
  useEffect(() => {
    const el = more.current;
    if (!el || drawn >= entries.length) return;
    if (typeof IntersectionObserver === "undefined") {
      setDrawn(entries.length);
      return;
    }
    const io = new IntersectionObserver((hit) => hit.some((h) => h.isIntersecting) && setDrawn((n) => n + DRAW_STEP), { rootMargin: "200px" });
    io.observe(el);
    return () => io.disconnect();
  }, [drawn, entries.length, total]);

  const now = new Date();
  const row = (chat: AskChat, extra?: { count?: number; open?: boolean; onToggle?: () => void; nested?: boolean }) => (
    <ChatRow
      key={chat.conversation_id}
      chat={chat}
      active={chat.conversation_id === current}
      renaming={renaming === chat.conversation_id}
      deleting={deleting === chat.conversation_id}
      count={extra?.count}
      expanded={extra?.open}
      onToggle={extra?.onToggle}
      nested={extra?.nested}
      onRename={(name) => {
        setRenaming(null);
        if (name !== null && name.trim() && name.trim() !== chat.title) change(chat, { name: name.trim() });
      }}
      onMenu={(what) => {
        if (what === "rename") setRenaming(chat.conversation_id);
        else if (what === "pin") change(chat, { pinned: !chat.pinned });
        else setDeleting(chat.conversation_id);
      }}
      onDelete={(sure) => {
        setDeleting(null);
        if (sure) change(chat, { deleted: true });
      }}
    />
  );
  const entry = (e: Entry) => {
    const id = e.head.conversation_id;
    const isOpen = open.has(id);
    return [
      row(e.head, {
        count: e.same.length ? e.same.length + 1 : undefined,
        open: isOpen,
        onToggle: () =>
          setOpen((cur) => {
            const next = new Set(cur);
            if (!next.delete(id)) next.add(id);
            return next;
          }),
      }),
      ...(isOpen ? e.same.map((c) => row(c, { nested: true })) : []),
    ];
  };

  const shown = entries.slice(0, drawn);
  const heading = "px-2.5 pb-1 text-[11px] font-semibold tracking-[0.02em] text-faint";
  const loading = list === null && !problem;

  return (
    <nav className={cn("flex min-h-0 flex-col bg-sidebar", className)} aria-label="Chats" data-testid="ask-chats">
      <div className="flex shrink-0 items-center gap-1.5 px-2.5 pt-2.5 pb-2">
        <button
          type="button"
          onClick={onNew}
          aria-label="New chat"
          title={`New chat (${newChatShortcut})`}
          className="flex h-8 min-w-0 flex-1 items-center justify-between gap-2 rounded-lg border border-border bg-surface px-2.5 text-[13px] font-medium text-fg shadow-sm transition-colors duration-150 hover:border-border-strong hover:bg-surface-2 cursor-pointer"
          data-testid="new-chat"
        >
          <span className="flex min-w-0 items-center gap-1.5">
            <Plus className="size-3.5 shrink-0 text-muted" strokeWidth={2.2} aria-hidden />
            <span className="truncate">New chat</span>
          </span>
          <kbd className="shrink-0 font-sans text-[10.5px] font-normal tracking-tight text-faint">{newChatShortcut}</kbd>
        </button>
        {onCollapse && (
          <button
            type="button"
            onClick={onCollapse}
            aria-label="Hide chats"
            title="Hide chats"
            data-testid="collapse-ask-chats"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg cursor-pointer"
          >
            <PanelLeftClose className="size-4" strokeWidth={1.85} aria-hidden />
          </button>
        )}
      </div>

      <div className="shrink-0 px-2.5 pb-2.5">
        <label className="flex h-8 items-center gap-2 rounded-lg border border-border bg-surface/80 px-2.5 text-[12.5px] text-fg transition-colors duration-150 focus-within:border-border-strong focus-within:bg-surface focus-within:ring-2 focus-within:ring-fg/10">
          <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value.slice(0, 200))}
            placeholder="Search chats"
            aria-label="Search chats"
            className="min-w-0 flex-1 bg-transparent text-[12.5px] text-fg outline-none focus-visible:outline-none placeholder:text-faint"
          />
          {q && (
            <button
              type="button"
              onClick={() => setQ("")}
              aria-label="Clear search"
              className="inline-flex size-5 shrink-0 items-center justify-center rounded text-faint hover:text-fg cursor-pointer"
            >
              <X className="size-3" strokeWidth={2.2} aria-hidden />
            </button>
          )}
        </label>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        {problem && (
          <p className="px-2.5 py-2 text-[12px] text-late" role="alert">
            {problem}
          </p>
        )}
        {loading && <ChatListSkeleton />}
        {list && list.available === false && (
          <p className="px-2.5 py-2 text-[12px] text-muted">Your chats can&apos;t be listed just now.</p>
        )}
        {list && list.available !== false && chats.length === 0 && (
          <div className="flex flex-col items-center gap-1.5 px-4 py-10 text-center" data-testid="ask-chats-empty">
            <MessageSquare className="size-4 text-faint" strokeWidth={1.8} aria-hidden />
            <p className="text-[12.5px] leading-5 text-muted">
              {q ? "No chat matches that." : "Your questions will be listed here."}
            </p>
          </div>
        )}
        <div className="space-y-4">
          {pinned.length > 0 && (
            <section aria-label="Pinned">
              <h3 className={heading}>Pinned</h3>
              <ul className="space-y-0.5">{pinned.map((c) => row(c))}</ul>
            </section>
          )}
          {GROUPS.map((g) => {
            const mine = shown.filter((e) => groupOf(e.head.last_at, now) === g);
            if (mine.length === 0) return null;
            return (
              <section key={g} aria-label={g}>
                <h3 className={heading}>{g}</h3>
                <ul className="space-y-0.5">{mine.flatMap(entry)}</ul>
              </section>
            );
          })}
        </div>
        {entries.length > drawn && <div ref={more} className="h-4" data-testid="more-chats" aria-hidden />}
      </div>
    </nav>
  );
}

function ChatRow({
  chat,
  active,
  renaming,
  deleting,
  onRename,
  onMenu,
  onDelete,
  count,
  expanded,
  onToggle,
  nested,
}: {
  count?: number;
  expanded?: boolean;
  onToggle?: () => void;
  /** An older chat shown under a collapsed row: it carries its own time, since the heading is the newest's. */
  nested?: boolean;
  chat: AskChat;
  active: boolean;
  renaming: boolean;
  deleting: boolean;
  onRename: (name: string | null) => void;
  onMenu: (what: "rename" | "pin" | "delete") => void;
  onDelete: (sure: boolean) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const displayTitle = cleanChatTitle(chat.title, chat.named);
  const age = compactAge(chat.last_at);
  useEffect(() => {
    if (renaming) input.current?.select();
  }, [renaming]);

  if (renaming) {
    return (
      <li className="px-1">
        <input
          ref={input}
          defaultValue={displayTitle}
          maxLength={120}
          aria-label="Chat name"
          className="h-8 w-full rounded-lg border border-border-strong bg-bg px-2.5 text-[13px] text-fg outline-none"
          onKeyDown={(e) => {
            if (e.key === "Enter") onRename(e.currentTarget.value);
            if (e.key === "Escape") onRename(null);
          }}
          onBlur={(e) => onRename(e.currentTarget.value)}
        />
      </li>
    );
  }
  if (deleting) {
    return (
      <li className="rounded-lg bg-surface-2/90 px-2.5 py-2 text-[12px]" role="group" aria-label="Delete this chat">
        <p className="truncate font-medium text-fg">{displayTitle}</p>
        <p className="mt-0.5 text-muted">Delete it from your chats? Your work log keeps the record.</p>
        <div className="mt-1.5 flex gap-3">
          <button type="button" className="font-medium text-late hover:underline cursor-pointer" onClick={() => onDelete(true)}>
            Delete
          </button>
          <button type="button" className="text-muted hover:text-fg cursor-pointer" onClick={() => onDelete(false)}>
            Cancel
          </button>
        </div>
      </li>
    );
  }

  const tooltipLines = [
    displayTitle,
    `${chat.questions === 1 ? "1 question" : `${chat.questions} questions`}, last ${formatDateTime(chat.last_at)}`,
    ...(count && count > 1 ? [`Asked ${count} times`] : []),
  ];

  return (
    <li
      className={cn(
        "group relative flex h-[32px] items-center overflow-hidden rounded-lg transition-colors duration-150",
        nested && "ml-3 border-l border-border pl-1",
        active ? "bg-row-on font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
      )}
    >
      <Link
        href={`/ask?c=${encodeURIComponent(chat.conversation_id)}`}
        className="flex h-full min-w-0 flex-1 items-center gap-2 px-2.5 text-[13px] leading-5"
        aria-current={active ? "page" : undefined}
        title={tooltipLines.join("\n")}
      >
        {chat.running && (
          <Loader2
            className="size-3.5 shrink-0 animate-spin text-accent"
            strokeWidth={2}
            role="status"
            aria-label="Working on an answer"
            data-testid="chat-running-indicator"
          />
        )}
        {chat.pinned && !chat.running && (
          <Pin className="size-3 shrink-0 text-faint" strokeWidth={2} aria-label="Pinned" />
        )}
        <span className="min-w-0 flex-1 truncate">{displayTitle}</span>
        {age && (
          <span
            className="shrink-0 text-[11px] font-normal text-faint tabular-nums transition-opacity duration-150 group-hover:opacity-0 group-focus-within:opacity-0 has-[[data-state=open]]:opacity-0"
            data-testid="chat-row-age"
          >
            {age}
          </span>
        )}
      </Link>

      {/* Smooth right-edge fade + compact Liquid Glass hover action pill so icons never collide with title text */}
      <div
        className={cn(
          "pointer-events-none absolute inset-y-0 right-0 flex items-center pl-6 pr-1 opacity-0 transition-opacity duration-150",
          "group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100 has-[[data-state=open]]:pointer-events-auto has-[[data-state=open]]:opacity-100",
          active
            ? "bg-row-on"
            : "bg-hover",
        )}
        data-testid="chat-row-actions"
      >
        <div className="liquid-glass-pill flex items-center gap-0.5 rounded-md p-0.5">
          {count && count > 1 && onToggle ? (
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={expanded}
              aria-label={expanded ? `Hide the ${count - 1} earlier times this was asked` : `Asked ${count} times: show the earlier ones`}
              title={expanded ? "Hide earlier threads" : `Show ${count - 1} earlier threads`}
              className="inline-flex h-5 items-center gap-0.5 rounded px-1 text-[10.5px] font-medium text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
            >
              <Layers className="size-3 shrink-0" strokeWidth={2} aria-hidden />
              <span className="num">{count}</span>
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => onMenu("pin")}
            aria-label={chat.pinned ? `Unpin ${displayTitle}` : `Pin ${displayTitle}`}
            title={chat.pinned ? "Unpin" : "Pin"}
            className={cn(
              "inline-flex size-5 items-center justify-center rounded text-faint hover:bg-surface-2 hover:text-fg cursor-pointer",
              chat.pinned && "text-fg",
            )}
          >
            <Pin className="size-3" strokeWidth={2} aria-hidden />
          </button>
          <button
            type="button"
            onClick={() => onMenu("delete")}
            aria-label={`Hide ${displayTitle}`}
            title="Hide"
            className="inline-flex size-5 items-center justify-center rounded text-faint hover:bg-surface-2 hover:text-fg cursor-pointer"
          >
            <EyeOff className="size-3" strokeWidth={2} aria-hidden />
          </button>
          <Menu.Root>
            <Menu.Trigger
              className="inline-flex size-5 items-center justify-center rounded text-faint hover:bg-surface-2 hover:text-fg cursor-pointer"
              aria-label={`More for ${displayTitle}`}
            >
              <Ellipsis className="size-3.5" strokeWidth={2} aria-hidden />
            </Menu.Trigger>
            <Menu.Portal>
              <Menu.Content align="end" sideOffset={4} className="z-50 min-w-32 rounded-lg border border-border-strong bg-raised p-1 shadow-pop">
                <Menu.Item onSelect={() => onMenu("rename")} className="flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover">
                  Rename
                </Menu.Item>
                <Menu.Item onSelect={() => onMenu("pin")} className="flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover">
                  {chat.pinned ? "Unpin" : "Pin"}
                </Menu.Item>
                <Menu.Item onSelect={() => onMenu("delete")} className="flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row text-late outline-none data-[highlighted]:bg-hover">
                  Delete
                </Menu.Item>
              </Menu.Content>
            </Menu.Portal>
          </Menu.Root>
        </div>
      </div>
    </li>
  );
}
