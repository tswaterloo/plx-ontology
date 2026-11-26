"use client";

import {
  ArrowUpRight,
  BarChart3,
  FileCode2,
  FileSpreadsheet,
  FileText,
  FolderUp,
  LayoutGrid,
  List,
  Pin,
  PinOff,
  Presentation,
  RotateCcw,
  Search,
  Sparkles,
  Trash2,
  Workflow,
  X,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Toolbar } from "@/components/ui/toolbar";
import { getAskChats, getAssistJobs, getInsights, type AssistJob } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate } from "@/lib/format";
import type { AskChat, InsightInfo } from "@/lib/types";

export type ArtifactFormatFilter = "all" | "Sheet" | "Doc" | "Slides" | "Chart" | "Workflow";
export type ArtifactViewMode = "grid" | "list";
export type ArtifactBadgeType = "Doc" | "Slides" | "Sheet" | "Chart" | "Workflow";
export type StudioFormatId = "sheets" | "docs" | "slides" | "charts" | "custom";
export type PetraDomain = "Finance" | "Purchasing" | "Operations" | "Quality" | "Sales";

export interface StudioFormatOption {
  id: StudioFormatId;
  label: string;
  shortLabel: string;
  subtitle: string;
  badgeType: ArtifactBadgeType;
  promptPrefix: string;
  placeholder: string;
}

export interface GalleryArtifact {
  id: string;
  title: string;
  subtitle: string;
  subtitleParts: string[];
  type: ArtifactBadgeType;
  date: string | null;
  href: string;
  openLabel: string;
  sourceKind: "chat" | "chart" | "assist" | "local";
  domain?: PetraDomain;
  pinned?: boolean;
}

export interface LocalPinnedArtifact {
  id: string;
  title: string;
  subtitle: string;
  type: ArtifactBadgeType;
  date: string;
  href: string;
  domain?: PetraDomain;
}

export interface SharePointOption {
  enabled: boolean;
  folder: string;
}

export const PETRA_DOMAINS: readonly PetraDomain[] = [
  "Operations",
  "Purchasing",
  "Finance",
  "Quality",
  "Sales",
] as const;

export const PAGE_SIZE = 12;
export const LOCAL_ARTIFACTS_KEY = "plx.artifacts.local.v1";
export const PINNED_IDS_KEY = "plx.artifacts.pinned-ids.v1";

export const STUDIO_FORMATS: readonly StudioFormatOption[] = [
  {
    id: "sheets",
    label: "Excel (.xlsx)",
    shortLabel: "Excel",
    subtitle: "Live spreadsheet or financial model with formulas",
    badgeType: "Sheet",
    promptPrefix: "Build an Excel spreadsheet (.xlsx) with formulas for ",
    placeholder: "Open PO landed cost tracker by supplier with variance formulas",
  },
  {
    id: "docs",
    label: "Word (.docx)",
    shortLabel: "Word",
    subtitle: "Formatted brief, memo, SOP, or PO/quote",
    badgeType: "Doc",
    promptPrefix: "Create a formatted Word document (.docx) for ",
    placeholder: "Q4 supplier quality brief covering late deliveries and QC holds",
  },
  {
    id: "slides",
    label: "Slides (.pptx)",
    shortLabel: "Slides",
    subtitle: "Executive slide deck with metrics and takeaways",
    badgeType: "Slides",
    promptPrefix: "Create a PowerPoint presentation (.pptx) summarizing ",
    placeholder: "Monthly operations review deck with top late POs and margin trend",
  },
  {
    id: "charts",
    label: "Chart",
    shortLabel: "Chart",
    subtitle: "Interactive chart and data breakdown",
    badgeType: "Chart",
    promptPrefix: "Create an interactive chart and breakdown of ",
    placeholder: "Late purchase orders by supplier over the last 90 days",
  },
  {
    id: "custom",
    label: "Custom",
    shortLabel: "Custom",
    subtitle: "Any file format, CSV export, PO/quote, or tailored report",
    badgeType: "Doc",
    promptPrefix: "Create a deliverable for ",
    placeholder: "Structured customer quote or custom CSV export from Business Central",
  },
] as const;

const STARTER_PROMPTS: readonly { label: string; format: StudioFormatId; domain: PetraDomain; prompt: string }[] = [
  {
    label: "Open PO landed cost tracker (.xlsx)",
    format: "sheets",
    domain: "Purchasing",
    prompt: "Open PO landed cost tracker grouped by supplier with unit cost, freight, and late-day flags",
  },
  {
    label: "Q4 supplier quality & QC hold brief (.docx)",
    format: "docs",
    domain: "Quality",
    prompt: "Supplier quality brief summarizing active QC holds, impacted batches, and corrective actions",
  },
  {
    label: "Monthly operations review deck (.pptx)",
    format: "slides",
    domain: "Operations",
    prompt: "Monthly operations review deck covering on-time delivery, open backlog, and key escalations",
  },
  {
    label: "Customer quote or purchase order draft",
    format: "custom",
    domain: "Sales",
    prompt: "Structured customer quote with SKU quantities, unit pricing, lead times, and delivery terms",
  },
];

const DELIVERABLE_CHAT_PATTERN =
  /\.(xlsx|docx|pptx|pdf|csv)\b|\b(workbook|spreadsheet|excel|powerpoint|slide\s*deck|slides|presentation|report|brief|memo|sop|purchase\s+order|po\s+draft|customer\s+quote|quote\s+for|tracker|chart|breakdown|export)\b/i;

/** Returns true only when an Ask conversation title indicates a real generated deliverable/file/chart. */
export function isDeliverableChat(chat: Pick<AskChat, "title">): boolean {
  const title = (chat.title ?? "").trim();
  if (!title) return false;
  return DELIVERABLE_CHAT_PATTERN.test(title);
}

export function inferChatArtifactType(title: string): ArtifactBadgeType {
  const lower = title.toLowerCase();
  if (/\b(pptx|slides?|deck|presentation)\b/.test(lower)) return "Slides";
  if (/\b(xlsx|excel|sheet|spreadsheet|tracker|model|margin|landed cost|workbook|csv)\b/.test(lower)) return "Sheet";
  if (/\b(chart|graph|trend|breakdown|plot|monthly revenue)\b/.test(lower)) return "Chart";
  if (/\b(workflow|schedule|recurring|assist)\b/.test(lower)) return "Workflow";
  return "Doc";
}

export function buildCreationPrompt(
  format: Pick<StudioFormatOption, "promptPrefix" | "placeholder">,
  description: string,
  domain: PetraDomain,
  sharepoint?: SharePointOption,
): string {
  const clean = description.trim() || format.placeholder;
  const base = `${format.promptPrefix}${clean} (${domain} team)`;
  if (sharepoint?.enabled) {
    const folder = sharepoint.folder.trim() || `Shared Documents/${domain}`;
    return `${base}. Prepare a SharePoint upload draft for folder "${folder}" (create the folder first if it does not exist).`;
  }
  return base;
}

function summarizeTitle(prompt: string): string {
  const line = prompt.replace(/\s+/g, " ").trim();
  if (line.length <= 72) return line;
  return `${line.slice(0, 69).trimEnd()}...`;
}

/** Clean deliverable titles so raw questions ending in "?" read as crisp artifact titles. */
export function formatDeliverableTitle(raw: string, keepQuestion = false): string {
  const collapsed = raw.replace(/\s+/g, " ").trim();
  if (!collapsed) return "Untitled deliverable";
  if (keepQuestion) return collapsed;
  return collapsed.replace(/\?+$/, "").trim() || collapsed;
}

function readLocalArtifacts(): LocalPinnedArtifact[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(LOCAL_ARTIFACTS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is LocalPinnedArtifact =>
        Boolean(item) &&
        typeof item.id === "string" &&
        typeof item.title === "string" &&
        typeof item.href === "string" &&
        typeof item.type === "string",
    );
  } catch {
    return [];
  }
}

function readPinnedIds(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(PINNED_IDS_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed.filter((v): v is string => typeof v === "string")) : new Set();
  } catch {
    return new Set();
  }
}

function writeLocalStorage(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage quota or restricted browsing context
  }
}

function FormatIcon({ id, className }: { id: StudioFormatId; className?: string }) {
  switch (id) {
    case "docs":
      return <FileText className={className} strokeWidth={1.8} aria-hidden />;
    case "slides":
      return <Presentation className={className} strokeWidth={1.8} aria-hidden />;
    case "sheets":
      return <FileSpreadsheet className={className} strokeWidth={1.8} aria-hidden />;
    case "charts":
      return <BarChart3 className={className} strokeWidth={1.8} aria-hidden />;
    case "custom":
      return <FileCode2 className={className} strokeWidth={1.8} aria-hidden />;
  }
}

function ArtifactTypeIcon({ type, className }: { type: ArtifactBadgeType; className?: string }) {
  switch (type) {
    case "Doc":
      return <FileText className={className} strokeWidth={1.8} aria-hidden />;
    case "Slides":
      return <Presentation className={className} strokeWidth={1.8} aria-hidden />;
    case "Sheet":
      return <FileSpreadsheet className={className} strokeWidth={1.8} aria-hidden />;
    case "Chart":
      return <BarChart3 className={className} strokeWidth={1.8} aria-hidden />;
    case "Workflow":
      return <Workflow className={className} strokeWidth={1.8} aria-hidden />;
  }
}

export function ArtifactsStudio({ intro }: { intro?: React.ReactNode } = {}) {
  const router = useRouter();
  const promptInputRef = useRef<HTMLTextAreaElement>(null);

  const [formatFilter, setFormatFilter] = useState<ArtifactFormatFilter>("all");
  const [view, setView] = useState<ArtifactViewMode>("grid");
  const [query, setQuery] = useState("");
  const [includeAskThreads, setIncludeAskThreads] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(PAGE_SIZE);
  const [loading, setLoading] = useState(true);

  const [chats, setChats] = useState<AskChat[]>([]);
  const [charts, setCharts] = useState<InsightInfo[]>([]);
  const [jobs, setJobs] = useState<AssistJob[]>([]);
  const [localArtifacts, setLocalArtifacts] = useState<LocalPinnedArtifact[]>([]);
  const [pinnedIds, setPinnedIds] = useState<Set<string>>(() => new Set());
  const [removedToast, setRemovedToast] = useState<{ artifact: LocalPinnedArtifact; index: number } | null>(null);

  // Prompt-first studio composer state
  const [selectedFormatId, setSelectedFormatId] = useState<StudioFormatId>("sheets");
  const [promptText, setPromptText] = useState("");
  const [domain, setDomain] = useState<PetraDomain>("Operations");
  const [sharePointEnabled, setSharePointEnabled] = useState(false);
  const [sharePointFolder, setSharePointFolder] = useState("Shared Documents/Operations");

  const selectedFormat = useMemo(
    () => STUDIO_FORMATS.find((f) => f.id === selectedFormatId) ?? STUDIO_FORMATS[0],
    [selectedFormatId],
  );

  useEffect(() => {
    setLocalArtifacts(readLocalArtifacts());
    setPinnedIds(readPinnedIds());
  }, []);

  useEffect(() => {
    setVisibleLimit(PAGE_SIZE);
  }, [formatFilter, query, includeAskThreads]);

  useEffect(() => {
    const ctl = new AbortController();
    setLoading(true);
    Promise.allSettled([
      getAskChats(undefined, ctl.signal),
      getInsights(ctl.signal),
      getAssistJobs(ctl.signal),
    ])
      .then(([chatsRes, insightsRes, jobsRes]) => {
        if (ctl.signal.aborted) return;
        if (chatsRes.status === "fulfilled" && Array.isArray(chatsRes.value?.chats)) {
          setChats(chatsRes.value.chats);
        }
        if (insightsRes.status === "fulfilled" && Array.isArray(insightsRes.value?.items)) {
          setCharts(insightsRes.value.items);
        }
        if (jobsRes.status === "fulfilled" && Array.isArray(jobsRes.value?.jobs)) {
          setJobs(jobsRes.value.jobs);
        }
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false);
      });
    return () => ctl.abort();
  }, []);

  const togglePin = useCallback((id: string) => {
    setPinnedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeLocalStorage(PINNED_IDS_KEY, Array.from(next));
      return next;
    });
  }, []);

  useEffect(() => {
    if (!removedToast) return;
    const t = window.setTimeout(() => setRemovedToast(null), 6000);
    return () => window.clearTimeout(t);
  }, [removedToast]);

  const removeLocalArtifact = useCallback((id: string) => {
    setLocalArtifacts((prev) => {
      const index = prev.findIndex((item) => item.id === id);
      if (index < 0) return prev;
      const removed = prev[index];
      setRemovedToast({ artifact: removed, index });
      const next = prev.filter((item) => item.id !== id);
      writeLocalStorage(LOCAL_ARTIFACTS_KEY, next);
      return next;
    });
  }, []);

  const undoRemoveLocalArtifact = useCallback(() => {
    setRemovedToast((cur) => {
      if (!cur) return null;
      setLocalArtifacts((prev) => {
        if (prev.some((item) => item.id === cur.artifact.id)) return prev;
        const next = [...prev];
        next.splice(Math.min(cur.index, next.length), 0, cur.artifact);
        writeLocalStorage(LOCAL_ARTIFACTS_KEY, next);
        return next;
      });
      return null;
    });
  }, []);

  const submitCreate = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const raw = promptText.trim();
      if (!raw) {
        promptInputRef.current?.focus();
        return;
      }
      const prompt = buildCreationPrompt(
        selectedFormat,
        raw,
        domain,
        sharePointEnabled ? { enabled: true, folder: sharePointFolder } : undefined,
      );
      const href = `/ask?q=${encodeURIComponent(prompt)}`;

      const created: LocalPinnedArtifact = {
        id: `local:${Date.now()}`,
        title: formatDeliverableTitle(summarizeTitle(raw)),
        subtitle: sharePointEnabled
          ? `${domain}, ${selectedFormat.label}, SharePoint`
          : `${domain}, ${selectedFormat.label}`,
        type: selectedFormat.badgeType,
        date: new Date().toISOString(),
        href,
        domain,
      };
      setLocalArtifacts((prev) => {
        const next = [created, ...prev].slice(0, 40);
        writeLocalStorage(LOCAL_ARTIFACTS_KEY, next);
        return next;
      });

      setPromptText("");
      router.push(href);
    },
    [domain, promptText, router, selectedFormat, sharePointEnabled, sharePointFolder],
  );

  const hiddenThreadCount = useMemo(
    () => chats.filter((c) => !isDeliverableChat(c)).length,
    [chats],
  );

  const allArtifacts = useMemo<GalleryArtifact[]>(() => {
    const items: GalleryArtifact[] = [];

    // 1. User-created deliverables from this studio
    for (const loc of localArtifacts) {
      const parts = loc.subtitle
        .split(/\s*(?:·|,)\s*/)
        .map((s) => s.trim())
        .filter(Boolean);
      items.push({
        id: loc.id,
        title: formatDeliverableTitle(loc.title),
        subtitle: parts.join(" "),
        subtitleParts: parts,
        type: loc.type,
        date: loc.date,
        href: loc.href,
        openLabel: "Open in Ask",
        sourceKind: "local",
        domain: loc.domain,
        pinned: true,
      });
    }

    // 2. Saved/pinned Insights charts ONLY (never dump all 80 catalog templates)
    for (const info of charts) {
      if (!info.saved) continue;
      const id = `chart:${info.id}`;
      const rawTitle =
        (info as InsightInfo & { title?: string }).title ||
        info.page_title ||
        info.question ||
        info.about ||
        info.id;
      const title = formatDeliverableTitle(rawTitle);
      const subtitleParts =
        info.about && info.about !== rawTitle && info.about !== title
          ? [info.about]
          : info.group
            ? [info.group, "Saved chart"]
            : ["Saved chart"];
      items.push({
        id,
        title,
        subtitle: subtitleParts.join(" "),
        subtitleParts,
        type: "Chart",
        date: info.last_run || null,
        href: `/insights#insight-${encodeURIComponent(info.id)}`,
        openLabel: "Open chart",
        sourceKind: "chart",
        pinned: pinnedIds.has(id),
      });
    }

    // 3. Assist jobs ONLY if they produced real prepared deliverables/pieces
    for (const job of jobs) {
      const preparedCount = (job.pieces ?? []).filter((p) => p.status === "prepared").length;
      if (preparedCount === 0) continue;
      const id = `assist:${job.job_id}`;
      const subtitleText = `${preparedCount} prepared ${preparedCount === 1 ? "deliverable" : "deliverables"}`;
      items.push({
        id,
        title: formatDeliverableTitle(job.summary || job.text || "Assist deliverable"),
        subtitle: subtitleText,
        subtitleParts: [subtitleText],
        type: "Workflow",
        date: job.requested_at || null,
        href: "/assist",
        openLabel: "Open in Assist",
        sourceKind: "assist",
        pinned: pinnedIds.has(id),
      });
    }

    // 4. Ask chats ONLY when they represent real files/reports/charts, unless user opts into all threads
    for (const chat of chats) {
      const isRealDeliverable = isDeliverableChat(chat);
      if (!isRealDeliverable && !includeAskThreads) continue;
      const id = `chat:${chat.conversation_id}`;
      const qCount = typeof chat.questions === "number" && Number.isFinite(chat.questions) ? chat.questions : 1;
      const subtitleParts = [
        isRealDeliverable ? "Generated in Ask" : "Ask thread",
        `${qCount} ${qCount === 1 ? "turn" : "turns"}`,
      ];
      items.push({
        id,
        title: formatDeliverableTitle(chat.title || "Untitled thread", !isRealDeliverable),
        subtitle: subtitleParts.join(" "),
        subtitleParts,
        type: inferChatArtifactType(chat.title || ""),
        date: chat.last_at || null,
        href: `/ask?c=${encodeURIComponent(chat.conversation_id)}`,
        openLabel: "Open in Ask",
        sourceKind: "chat",
        pinned: Boolean(chat.pinned) || pinnedIds.has(id),
      });
    }

    return items.sort((a, b) => {
      if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1;
      const ta = a.date ? Date.parse(a.date) || 0 : 0;
      const tb = b.date ? Date.parse(b.date) || 0 : 0;
      return tb - ta;
    });
  }, [charts, chats, includeAskThreads, jobs, localArtifacts, pinnedIds]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return allArtifacts.filter((item) => {
      if (formatFilter !== "all" && item.type !== formatFilter) return false;
      if (!q) return true;
      return (
        item.title.toLowerCase().includes(q) ||
        item.subtitle.toLowerCase().includes(q) ||
        item.type.toLowerCase().includes(q) ||
        (item.domain?.toLowerCase().includes(q) ?? false)
      );
    });
  }, [allArtifacts, formatFilter, query]);

  const paged = useMemo(() => filtered.slice(0, visibleLimit), [filtered, visibleLimit]);
  const remaining = Math.max(0, filtered.length - paged.length);

  const formatFilters: { id: ArtifactFormatFilter; label: string }[] = [
    { id: "all", label: "All" },
    { id: "Sheet", label: "Sheets" },
    { id: "Doc", label: "Docs" },
    { id: "Slides", label: "Slides" },
    { id: "Chart", label: "Charts" },
    { id: "Workflow", label: "Workflows" },
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg" data-testid="artifacts-studio">
      <Toolbar heading title="Artifacts" />
      {intro}

      <div className="mx-auto w-full max-w-[1160px] flex-1 space-y-9 px-4 py-6 sm:px-6 lg:px-8">
        {/* Prompt-first "Create new deliverable" Studio Composer */}
        <section
          aria-labelledby="create-deliverable-heading"
          data-testid="create-deliverable-studio"
          className="space-y-3"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2
              id="create-deliverable-heading"
              className="text-[18px] font-semibold tracking-[-0.02em] text-fg"
            >
              Create new deliverable
            </h2>
            <span className="text-[12.5px] text-faint">
              Built from live company records in Ask, with optional SharePoint handoff
            </span>
          </div>

          <form
            onSubmit={submitCreate}
            className="rounded-xl border border-border-strong bg-surface p-4 shadow-sm transition-colors focus-within:border-accent/60"
          >
            <label htmlFor="artifact-prompt-input" className="sr-only">
              Describe what you want to create
            </label>
            <textarea
              ref={promptInputRef}
              id="artifact-prompt-input"
              rows={3}
              value={promptText}
              onChange={(e) => setPromptText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && promptText.trim()) {
                  e.preventDefault();
                  submitCreate(e);
                }
              }}
              placeholder="Describe what you want to create (Excel tracker, Word brief, slide deck, chart, PO/quote, or any custom file)..."
              data-testid="artifact-prompt-input"
              className="w-full resize-none bg-transparent text-[14.5px] leading-[1.6] text-fg placeholder:text-faint focus:outline-none"
            />

            {/* Liquid Glass Format Selector + Controls Row */}
            <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
              <div
                role="group"
                aria-label="Deliverable format"
                className="liquid-glass-pill inline-flex flex-wrap items-center gap-0.5 rounded-lg p-0.5"
                data-testid="artifact-format-pills"
              >
                {STUDIO_FORMATS.map((fmt) => {
                  const active = fmt.id === selectedFormatId;
                  return (
                    <button
                      key={fmt.id}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setSelectedFormatId(fmt.id)}
                      data-testid={`studio-format-${fmt.id}`}
                      className={cn(
                        "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-all cursor-pointer",
                        active ? "liquid-glass-active" : "text-muted hover:text-fg",
                      )}
                    >
                      <FormatIcon id={fmt.id} className="size-3.5" />
                      <span>{fmt.label}</span>
                    </button>
                  );
                })}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <label className="inline-flex items-center gap-1.5 text-[12px] text-muted">
                  <span className="sr-only">Team domain</span>
                  <select
                    aria-label="Team domain"
                    value={domain}
                    onChange={(e) => {
                      const next = e.target.value as PetraDomain;
                      setDomain(next);
                      if (sharePointFolder.startsWith("Shared Documents/")) {
                        setSharePointFolder(`Shared Documents/${next}`);
                      }
                    }}
                    data-testid="artifact-domain-select"
                    className="h-8 cursor-pointer rounded-md border border-border bg-surface px-2.5 text-[12.5px] font-medium text-fg hover:border-border-strong focus:border-accent/60 focus:outline-none"
                  >
                    {PETRA_DOMAINS.map((d) => (
                      <option key={d} value={d} className="bg-raised text-fg">
                        {d}
                      </option>
                    ))}
                  </select>
                </label>

                <button
                  type="button"
                  role="switch"
                  aria-checked={sharePointEnabled}
                  onClick={() => setSharePointEnabled((v) => !v)}
                  data-testid="artifact-sharepoint-toggle"
                  className={cn(
                    "inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                    sharePointEnabled
                      ? "border-border-strong bg-surface-2 text-fg"
                      : "border-border bg-bg text-muted hover:border-border-strong hover:text-fg",
                  )}
                >
                  <FolderUp className="size-3.5" aria-hidden />
                  <span>Save to SharePoint</span>
                </button>

                <Button
                  type="submit"
                  variant="primary"
                  size="md"
                  disabled={!promptText.trim()}
                  data-testid="artifact-create-submit"
                >
                  <span>Create</span>
                  <ArrowUpRight className="size-3.5" aria-hidden />
                </Button>
              </div>
            </div>

            {sharePointEnabled && (
              <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3 text-[12.5px]">
                <label htmlFor="artifact-sp-folder" className="shrink-0 font-medium text-muted">
                  SharePoint folder
                </label>
                <input
                  id="artifact-sp-folder"
                  type="text"
                  value={sharePointFolder}
                  onChange={(e) => setSharePointFolder(e.target.value)}
                  placeholder="Shared Documents/Operations"
                  data-testid="artifact-sharepoint-folder"
                  className="h-8 min-w-[220px] flex-1 rounded-md border border-border-strong bg-bg px-2.5 text-[12.5px] text-fg placeholder:text-faint focus:border-accent/60 focus:outline-none"
                />
                <span className="text-[11.5px] text-faint">
                  Prepares an upload draft for your confirmation
                </span>
              </div>
            )}
          </form>

          {/* Quiet spaced clickable starter pills */}
          <div className="flex flex-wrap items-center gap-2 pt-0.5">
            <span className="text-[12px] text-faint">Try:</span>
            {STARTER_PROMPTS.map((starter) => (
              <button
                key={starter.label}
                type="button"
                onClick={() => {
                  setSelectedFormatId(starter.format);
                  setDomain(starter.domain);
                  setPromptText(starter.prompt);
                  if (sharePointFolder.startsWith("Shared Documents/")) {
                    setSharePointFolder(`Shared Documents/${starter.domain}`);
                  }
                  promptInputRef.current?.focus();
                }}
                className="cursor-pointer rounded-md border border-border/70 px-2.5 py-1 text-left text-[12px] text-muted transition-colors hover:border-border-strong hover:text-fg"
              >
                {starter.label}
              </button>
            ))}
          </div>
        </section>

        {/* Deliverables Gallery */}
        <section
          aria-labelledby="gallery-heading"
          data-testid="artifacts-gallery"
          className="space-y-4 border-t border-border pt-7"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-baseline gap-2.5">
              <h2 id="gallery-heading" className="text-[15px] font-semibold tracking-[-0.01em] text-fg">
                Deliverables
              </h2>
              {!loading && (
                <span className="num text-[12px] text-faint" data-testid="artifacts-count">
                  Showing {paged.length} of {filtered.length} {filtered.length === 1 ? "item" : "items"}
                </span>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {hiddenThreadCount > 0 && (
                <label className="inline-flex cursor-pointer items-center gap-1.5 text-[12px] text-muted select-none hover:text-fg">
                  <input
                    type="checkbox"
                    checked={includeAskThreads}
                    onChange={(e) => setIncludeAskThreads(e.target.checked)}
                    data-testid="artifacts-include-threads"
                    className="size-3.5 cursor-pointer rounded border-border-strong accent-[var(--accent)]"
                  />
                  <span>Include Ask threads ({hiddenThreadCount})</span>
                </label>
              )}

              <div className="relative w-52 sm:w-60">
                <Search
                  className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint"
                  strokeWidth={2}
                  aria-hidden
                />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search deliverables"
                  aria-label="Search deliverables"
                  data-testid="artifacts-search"
                  className="h-8 w-full rounded-md border border-border bg-surface pr-7 pl-8 text-[12.5px] text-fg placeholder:text-faint focus:border-border-strong focus:outline-none"
                />
                {query && (
                  <button
                    type="button"
                    onClick={() => setQuery("")}
                    aria-label="Clear search"
                    className="absolute top-1/2 right-1.5 flex size-5 -translate-y-1/2 items-center justify-center rounded text-muted hover:bg-hover hover:text-fg cursor-pointer"
                  >
                    <X className="size-3.5" aria-hidden />
                  </button>
                )}
              </div>

              <div
                className="inline-flex items-center rounded-md border border-border bg-surface p-0.5"
                role="group"
                aria-label="View mode"
              >
                <button
                  type="button"
                  aria-pressed={view === "grid"}
                  aria-label="Grid view"
                  onClick={() => setView("grid")}
                  data-testid="artifacts-view-grid"
                  className={cn(
                    "inline-flex size-7 items-center justify-center rounded-sm transition-colors cursor-pointer",
                    view === "grid" ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
                  )}
                >
                  <LayoutGrid className="size-3.5" strokeWidth={2} aria-hidden />
                </button>
                <button
                  type="button"
                  aria-pressed={view === "list"}
                  aria-label="List view"
                  onClick={() => setView("list")}
                  data-testid="artifacts-view-list"
                  className={cn(
                    "inline-flex size-7 items-center justify-center rounded-sm transition-colors cursor-pointer",
                    view === "list" ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
                  )}
                >
                  <List className="size-3.5" strokeWidth={2} aria-hidden />
                </button>
              </div>
            </div>
          </div>

          {removedToast && (
            <div
              role="status"
              data-testid="artifact-removed-toast"
              className="flex items-center justify-between gap-3 rounded-lg border border-border bg-surface px-3.5 py-2 text-[12.5px] text-fg"
            >
              <span className="truncate">Removed &ldquo;{removedToast.artifact.title}&rdquo;</span>
              <div className="flex shrink-0 items-center gap-1.5">
                <button
                  type="button"
                  onClick={undoRemoveLocalArtifact}
                  data-testid="artifact-removed-undo"
                  className="inline-flex h-6 items-center gap-1 rounded-md border border-border-strong bg-bg px-2 text-[12px] font-medium text-fg transition-colors hover:bg-hover cursor-pointer"
                >
                  <RotateCcw className="size-3" strokeWidth={2} aria-hidden />
                  <span>Undo</span>
                </button>
                <button
                  type="button"
                  aria-label="Dismiss notice"
                  onClick={() => setRemovedToast(null)}
                  className="inline-flex size-5 items-center justify-center rounded text-muted hover:bg-hover hover:text-fg cursor-pointer"
                >
                  <X className="size-3" aria-hidden />
                </button>
              </div>
            </div>
          )}

          {/* Liquid Glass format filter */}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div
              role="group"
              aria-label="Filter by format"
              className="liquid-glass-pill inline-flex flex-wrap items-center gap-0.5 rounded-lg p-0.5"
              data-testid="artifacts-format-filter"
            >
              {formatFilters.map((f) => {
                const active = formatFilter === f.id;
                return (
                  <button
                    key={f.id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => setFormatFilter(f.id)}
                    data-testid={`artifacts-format-${f.id.toLowerCase()}`}
                    className={cn(
                      "inline-flex h-7 items-center rounded-md px-2.5 text-[12px] font-medium transition-all cursor-pointer",
                      active ? "liquid-glass-active" : "text-muted hover:text-fg",
                    )}
                  >
                    {f.label}
                  </button>
                );
              })}
            </div>
          </div>

          {loading ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-busy="true" data-testid="artifacts-loading">
              {[0, 1, 2, 3, 4, 5].map((idx) => (
                <div key={idx} className="space-y-3 rounded-lg border border-border bg-surface p-4">
                  <div className="h-3 w-16 rounded bg-surface-2 skeleton-shimmer" />
                  <div className="h-4 w-3/4 rounded bg-surface-2 skeleton-shimmer" />
                  <div className="h-3 w-1/2 rounded bg-surface-2 skeleton-shimmer" />
                </div>
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <div
              className="flex flex-col items-center justify-center border-y border-border px-6 py-12 text-center"
              data-testid="artifacts-empty"
            >
              <Sparkles className="mb-2.5 size-4 text-muted" strokeWidth={1.8} aria-hidden />
              <p className="text-[14px] font-medium text-fg">
                {query.trim() ? `No deliverables match "${query.trim()}"` : "No deliverables saved here yet"}
              </p>
              <p className="mt-1 max-w-[44ch] text-[13px] text-muted">
                {query.trim() || formatFilter !== "all"
                  ? "Clear the search or switch format filters to browse everything available."
                  : "Describe an Excel tracker, Word brief, slide deck, or chart above to generate your first deliverable."}
              </p>
              {(query.trim() || formatFilter !== "all") && (
                <div className="mt-4">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setQuery("");
                      setFormatFilter("all");
                    }}
                  >
                    Clear filters
                  </Button>
                </div>
              )}
            </div>
          ) : view === "grid" ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="artifacts-grid">
              {paged.map((item) => (
                <article
                  key={item.id}
                  data-testid="artifact-card"
                  className="group flex flex-col justify-between gap-4 rounded-lg border border-border bg-surface p-4 transition-[border-color] duration-150 hover:border-border-strong"
                >
                  <div className="space-y-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted">
                        <ArtifactTypeIcon type={item.type} className="size-3.5 text-muted" />
                        <span>{item.type}</span>
                      </span>
                      {item.sourceKind === "local" ? (
                        <button
                          type="button"
                          onClick={() => removeLocalArtifact(item.id)}
                          aria-label={`Remove ${item.title}`}
                          title="Remove"
                          className="flex size-6 shrink-0 items-center justify-center rounded-md text-faint opacity-70 transition-colors hover:bg-hover hover:text-late hover:opacity-100 cursor-pointer"
                        >
                          <Trash2 className="size-3.5" aria-hidden />
                        </button>
                      ) : (
                        <button
                          type="button"
                          onClick={() => togglePin(item.id)}
                          aria-label={item.pinned ? `Unpin ${item.title}` : `Pin ${item.title}`}
                          title={item.pinned ? "Unpin" : "Pin"}
                          className={cn(
                            "flex size-6 shrink-0 items-center justify-center rounded-md transition-colors cursor-pointer",
                            item.pinned
                              ? "text-fg hover:bg-hover"
                              : "text-faint opacity-70 hover:bg-hover hover:text-fg hover:opacity-100",
                          )}
                        >
                          {item.pinned ? <PinOff className="size-3.5" aria-hidden /> : <Pin className="size-3.5" aria-hidden />}
                        </button>
                      )}
                    </div>

                    <div className="space-y-1">
                      <h3 className="line-clamp-2 text-[14.5px] leading-[20px] font-semibold tracking-[-0.01em] text-fg">
                        {item.title}
                      </h3>
                      {item.subtitleParts.length > 0 && (
                        <p className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[12px] leading-[18px] text-muted">
                          {item.subtitleParts.map((part, idx) => (
                            <span key={`${part}-${idx}`}>{part}</span>
                          ))}
                        </p>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/80 pt-2.5">
                    <span className="num text-[11.5px] text-faint">
                      {item.date ? formatDate(item.date, { short: true }) : "Saved"}
                    </span>

                    <Link
                      href={item.href}
                      className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-medium text-fg transition-colors hover:bg-surface-2"
                    >
                      <span>{item.openLabel}</span>
                      <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
                    </Link>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div
              className="divide-y divide-border border-y border-border"
              data-testid="artifacts-list"
            >
              {paged.map((item) => (
                <div
                  key={item.id}
                  data-testid="artifact-row"
                  className="flex flex-wrap items-center gap-3 px-2 py-3 transition-colors hover:bg-row-on"
                >
                  <ArtifactTypeIcon type={item.type} className="size-4 shrink-0 text-muted" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13.5px] font-medium text-fg">{item.title}</p>
                    {item.subtitleParts.length > 0 && (
                      <p className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[12px] text-muted">
                        {item.subtitleParts.map((part, idx) => (
                          <span key={`${part}-${idx}`}>{part}</span>
                        ))}
                      </p>
                    )}
                  </div>
                  <span className="text-[12px] font-medium text-muted">
                    {item.type}
                  </span>
                  <span className="num hidden w-20 text-right text-[12px] text-faint sm:inline">
                    {item.date ? formatDate(item.date, { short: true }) : "Saved"}
                  </span>
                  {item.sourceKind === "local" ? (
                    <button
                      type="button"
                      onClick={() => removeLocalArtifact(item.id)}
                      aria-label={`Remove ${item.title}`}
                      title="Remove"
                      className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-surface-2 hover:text-late cursor-pointer"
                    >
                      <Trash2 className="size-3.5" aria-hidden />
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => togglePin(item.id)}
                      aria-label={item.pinned ? `Unpin ${item.title}` : `Pin ${item.title}`}
                      title={item.pinned ? "Unpin" : "Pin"}
                      className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
                    >
                      {item.pinned ? <PinOff className="size-3.5" aria-hidden /> : <Pin className="size-3.5" aria-hidden />}
                    </button>
                  )}
                  <Link
                    href={item.href}
                    className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-fg transition-colors hover:bg-surface-2"
                  >
                    <span>{item.openLabel}</span>
                    <ArrowUpRight className="size-3 text-muted" aria-hidden />
                  </Link>
                </div>
              ))}
            </div>
          )}

          {remaining > 0 && !loading && (
            <div className="flex justify-center pt-2">
              <Button
                type="button"
                variant="secondary"
                size="md"
                onClick={() => setVisibleLimit((n) => n + PAGE_SIZE)}
                data-testid="artifacts-show-more"
              >
                Show more ({remaining} remaining)
              </Button>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
