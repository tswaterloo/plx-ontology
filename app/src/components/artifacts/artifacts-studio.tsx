"use client";

import {
  ArrowUpRight,
  BarChart3,
  FileCode2,
  FileSpreadsheet,
  FileText,
  FolderPlus,
  FolderUp,
  LayoutGrid,
  List,
  Pin,
  PinOff,
  Presentation,
  Search,
  Sparkles,
  Workflow,
  X,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cleanChatTitle } from "@/components/ask-chats";
import { Button } from "@/components/ui/button";
import { Toolbar } from "@/components/ui/toolbar";
import {
  getAskChats,
  getAssistJobs,
  getInsights,
  getSharePointFolders,
  type AssistJob,
  type SharePointFolderInfo,
} from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate } from "@/lib/format";
import type { AskChat, InsightInfo } from "@/lib/types";
import { DeliverableArtwork } from "./deliverable-artwork";
import { SharePointUploadPopover } from "./sharepoint-upload-popover";

export type ArtifactFormatFilter = "all" | "Sheet" | "Doc" | "Slides" | "Chart" | "Workflow";
export type ArtifactViewMode = "grid" | "list";
export type ArtifactBadgeType = "Doc" | "Slides" | "Sheet" | "Chart" | "Workflow";
export type StudioFormatId = "sheets" | "docs" | "slides" | "charts" | "custom";
export type PetraDomain = "Finance" | "Purchasing" | "Operations" | "Quality" | "Sales";

export interface StudioFormatOption {
  id: StudioFormatId;
  title: string;
  extension: string;
  label: string;
  shortLabel: string;
  subtitle: string;
  badgeType: ArtifactBadgeType;
  defaultDomain: PetraDomain;
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
  sourceKind: "chat" | "chart" | "assist";
  domain?: PetraDomain;
  pinned?: boolean;
}

export interface SharePointOption {
  enabled: boolean;
  folder: string;
  createIfMissing?: boolean;
}

export const PETRA_DOMAINS: readonly PetraDomain[] = [
  "Operations",
  "Purchasing",
  "Finance",
  "Quality",
  "Sales",
] as const;

export const PAGE_SIZE = 12;
export const PINNED_IDS_KEY = "plx.artifacts.pinned-ids.v1";

const FALLBACK_SHAREPOINT_FOLDERS: SharePointFolderInfo[] = [
  { path: "Shared Documents/Operations", label: "Operations", drive_id: "", parent_id: "", site: "Company Library", file_count: 0, source: "default" },
  { path: "Shared Documents/Finance", label: "Finance", drive_id: "", parent_id: "", site: "Company Library", file_count: 0, source: "default" },
  { path: "Shared Documents/Purchasing", label: "Purchasing", drive_id: "", parent_id: "", site: "Company Library", file_count: 0, source: "default" },
  { path: "Shared Documents/Quality", label: "Quality", drive_id: "", parent_id: "", site: "Company Library", file_count: 0, source: "default" },
  { path: "Shared Documents/Sales", label: "Sales", drive_id: "", parent_id: "", site: "Company Library", file_count: 0, source: "default" },
];

export const STUDIO_FORMATS: readonly StudioFormatOption[] = [
  {
    id: "sheets",
    title: "Excel Workbook",
    extension: ".xlsx",
    label: "Excel (.xlsx)",
    shortLabel: "Excel",
    subtitle: "Financial model, tracker, or variance workbook with formulas",
    badgeType: "Sheet",
    defaultDomain: "Purchasing",
    promptPrefix: "Build an Excel spreadsheet (.xlsx) with formulas for ",
    placeholder: "Open PO landed cost tracker by supplier with variance formulas",
  },
  {
    id: "docs",
    title: "Word Brief / SOP",
    extension: ".docx",
    label: "Word (.docx)",
    shortLabel: "Word",
    subtitle: "Executive brief, supplier memo, or structured SOP",
    badgeType: "Doc",
    defaultDomain: "Quality",
    promptPrefix: "Create a formatted Word document (.docx) for ",
    placeholder: "Q4 supplier quality brief covering late deliveries and QC holds",
  },
  {
    id: "slides",
    title: "Slide Deck",
    extension: ".pptx",
    label: "Slides (.pptx)",
    shortLabel: "Slides",
    subtitle: "16:9 executive deck with KPI summary and takeaways",
    badgeType: "Slides",
    defaultDomain: "Operations",
    promptPrefix: "Create a PowerPoint presentation (.pptx) summarizing ",
    placeholder: "Monthly operations review deck with top late POs and margin trend",
  },
  {
    id: "charts",
    title: "Live Chart / Report",
    extension: "Chart",
    label: "Chart",
    shortLabel: "Chart",
    subtitle: "Interactive trend breakdown from live company records",
    badgeType: "Chart",
    defaultDomain: "Finance",
    promptPrefix: "Create an interactive chart and breakdown of ",
    placeholder: "Late purchase orders by supplier over the last 90 days",
  },
  {
    id: "custom",
    title: "PO & Quotes",
    extension: ".docx",
    label: "PO / Quote",
    shortLabel: "PO / Quote",
    subtitle: "Commercial quote, PO draft, CSV export, or any custom file",
    badgeType: "Doc",
    defaultDomain: "Sales",
    promptPrefix: "Create a structured deliverable for ",
    placeholder: "Customer quote with SKU quantities, unit pricing, lead times, and delivery terms",
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
    const createClause =
      sharepoint.createIfMissing === false
        ? ""
        : " (create the folder first if it does not exist)";
    return `${base}. Prepare a SharePoint upload draft for folder "${folder}"${createClause}.`;
  }
  return base;
}

/** Clean deliverable titles so raw questions ending in "?" read as crisp artifact titles. */
export function formatDeliverableTitle(raw: string, keepQuestion = false): string {
  const collapsed = raw.replace(/\s+/g, " ").trim();
  if (!collapsed) return "Untitled deliverable";
  if (keepQuestion) return collapsed;
  return collapsed.replace(/\?+$/, "").trim() || collapsed;
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

/** Miniature geometric header preview strip for each deliverable card on the 3-column visual shelf. */
function DeliverableShelfPreview({ type }: { type: ArtifactBadgeType }) {
  switch (type) {
    case "Sheet":
      return (
        <svg viewBox="0 0 320 76" fill="none" className="h-full w-full" aria-hidden="true">
          <rect x="16" y="12" width="288" height="56" rx="4" fill="currentColor" fillOpacity="0.03" stroke="currentColor" strokeOpacity="0.10" />
          <rect x="16" y="12" width="288" height="14" rx="4" fill="currentColor" fillOpacity="0.06" />
          <line x1="16" y1="26" x2="304" y2="26" stroke="currentColor" strokeOpacity="0.10" />
          <line x1="16" y1="40" x2="304" y2="40" stroke="currentColor" strokeOpacity="0.06" />
          <line x1="16" y1="54" x2="304" y2="54" stroke="currentColor" strokeOpacity="0.06" />
          <line x1="112" y1="12" x2="112" y2="68" stroke="currentColor" strokeOpacity="0.07" />
          <line x1="184" y1="12" x2="184" y2="68" stroke="currentColor" strokeOpacity="0.07" />
          <line x1="248" y1="12" x2="248" y2="68" stroke="currentColor" strokeOpacity="0.07" />
          <rect x="26" y="17" width="44" height="4" rx="1.5" fill="currentColor" fillOpacity="0.32" />
          <rect x="122" y="17" width="34" height="4" rx="1.5" fill="currentColor" fillOpacity="0.24" />
          <rect x="194" y="17" width="38" height="4" rx="1.5" fill="currentColor" fillOpacity="0.24" />
          <rect x="26" y="31" width="60" height="3.5" rx="1.5" fill="currentColor" fillOpacity="0.22" />
          <rect x="122" y="31" width="40" height="3.5" rx="1.5" fill="currentColor" fillOpacity="0.16" />
          <rect x="26" y="45" width="52" height="3.5" rx="1.5" fill="currentColor" fillOpacity="0.18" />
          <rect x="194" y="45" width="36" height="3.5" rx="1.5" fill="currentColor" fillOpacity="0.16" />
          <rect x="249" y="54.5" width="54" height="13" fill="var(--ok)" fillOpacity="0.16" />
          <rect x="258" y="59" width="34" height="4" rx="1.5" fill="var(--ok)" fillOpacity="0.85" />
          <rect x="26" y="59" width="38" height="4" rx="1.5" fill="currentColor" fillOpacity="0.35" />
        </svg>
      );
    case "Slides":
      return (
        <svg viewBox="0 0 320 76" fill="none" className="h-full w-full" aria-hidden="true">
          <rect x="34" y="14" width="252" height="54" rx="4" fill="currentColor" fillOpacity="0.02" stroke="currentColor" strokeOpacity="0.07" />
          <rect x="24" y="10" width="272" height="56" rx="4" fill="currentColor" fillOpacity="0.035" stroke="currentColor" strokeOpacity="0.12" />
          <rect x="36" y="18" width="92" height="4.5" rx="1.5" fill="currentColor" fillOpacity="0.45" />
          <rect x="256" y="18" width="28" height="4" rx="1.5" fill="currentColor" fillOpacity="0.18" />
          <line x1="36" y1="28" x2="284" y2="28" stroke="currentColor" strokeOpacity="0.08" />
          <rect x="36" y="34" width="76" height="24" rx="3" fill="currentColor" fillOpacity="0.04" stroke="currentColor" strokeOpacity="0.08" />
          <rect x="44" y="41" width="28" height="3" rx="1.5" fill="currentColor" fillOpacity="0.22" />
          <rect x="44" y="48" width="44" height="5" rx="1.5" fill="var(--risk)" fillOpacity="0.75" />
          <rect x="120" y="34" width="76" height="24" rx="3" fill="currentColor" fillOpacity="0.04" stroke="currentColor" strokeOpacity="0.08" />
          <rect x="128" y="41" width="32" height="3" rx="1.5" fill="currentColor" fillOpacity="0.22" />
          <rect x="128" y="48" width="48" height="5" rx="1.5" fill="currentColor" fillOpacity="0.40" />
          <rect x="204" y="34" width="80" height="24" rx="3" fill="currentColor" fillOpacity="0.04" stroke="currentColor" strokeOpacity="0.08" />
          <rect x="212" y="47" width="10" height="7" rx="1" fill="currentColor" fillOpacity="0.20" />
          <rect x="226" y="43" width="10" height="11" rx="1" fill="currentColor" fillOpacity="0.28" />
          <rect x="240" y="39" width="10" height="15" rx="1" fill="var(--accent)" fillOpacity="0.75" />
          <rect x="254" y="42" width="10" height="12" rx="1" fill="currentColor" fillOpacity="0.25" />
        </svg>
      );
    case "Chart":
      return (
        <svg viewBox="0 0 320 76" fill="none" className="h-full w-full" aria-hidden="true">
          <line x1="20" y1="20" x2="300" y2="20" stroke="currentColor" strokeOpacity="0.06" strokeDasharray="2 3" />
          <line x1="20" y1="38" x2="300" y2="38" stroke="currentColor" strokeOpacity="0.06" strokeDasharray="2 3" />
          <line x1="20" y1="58" x2="300" y2="58" stroke="currentColor" strokeOpacity="0.12" />
          <rect x="34" y="42" width="22" height="16" rx="2" fill="currentColor" fillOpacity="0.14" />
          <rect x="70" y="34" width="22" height="24" rx="2" fill="currentColor" fillOpacity="0.18" />
          <rect x="106" y="38" width="22" height="20" rx="2" fill="currentColor" fillOpacity="0.18" />
          <rect x="142" y="28" width="22" height="30" rx="2" fill="currentColor" fillOpacity="0.24" />
          <rect x="178" y="24" width="22" height="34" rx="2" fill="var(--accent)" fillOpacity="0.55" />
          <rect x="214" y="18" width="22" height="40" rx="2" fill="var(--accent)" fillOpacity="0.80" />
          <rect x="250" y="22" width="22" height="36" rx="2" fill="var(--accent)" fillOpacity="0.65" />
          <path d="M 45 38 L 81 30 L 117 34 L 153 24 L 189 20 L 225 14 L 261 17" stroke="var(--ok)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          <circle cx="225" cy="14" r="2.6" fill="var(--ok)" />
        </svg>
      );
    case "Workflow":
      return (
        <svg viewBox="0 0 320 76" fill="none" className="h-full w-full" aria-hidden="true">
          <line x1="88" y1="38" x2="128" y2="38" stroke="currentColor" strokeOpacity="0.22" strokeWidth="1.2" />
          <line x1="192" y1="38" x2="232" y2="38" stroke="currentColor" strokeOpacity="0.22" strokeWidth="1.2" />
          <rect x="24" y="24" width="64" height="28" rx="4" fill="currentColor" fillOpacity="0.04" stroke="currentColor" strokeOpacity="0.14" />
          <rect x="34" y="33" width="32" height="3.5" rx="1.5" fill="currentColor" fillOpacity="0.35" />
          <rect x="34" y="40" width="44" height="3" rx="1.5" fill="currentColor" fillOpacity="0.18" />
          <rect x="128" y="24" width="64" height="28" rx="4" fill="var(--accent)" fillOpacity="0.10" stroke="var(--accent)" strokeOpacity="0.45" />
          <rect x="138" y="33" width="38" height="3.5" rx="1.5" fill="var(--accent)" fillOpacity="0.85" />
          <rect x="138" y="40" width="28" height="3" rx="1.5" fill="currentColor" fillOpacity="0.25" />
          <rect x="232" y="24" width="64" height="28" rx="4" fill="var(--ok)" fillOpacity="0.08" stroke="var(--ok)" strokeOpacity="0.38" />
          <rect x="242" y="33" width="36" height="3.5" rx="1.5" fill="var(--ok)" fillOpacity="0.85" />
          <rect x="242" y="40" width="40" height="3" rx="1.5" fill="currentColor" fillOpacity="0.22" />
        </svg>
      );
    case "Doc":
    default:
      return (
        <svg viewBox="0 0 320 76" fill="none" className="h-full w-full" aria-hidden="true">
          <rect x="36" y="10" width="248" height="58" rx="4" fill="currentColor" fillOpacity="0.03" stroke="currentColor" strokeOpacity="0.10" />
          <rect x="52" y="18" width="110" height="5" rx="1.5" fill="currentColor" fillOpacity="0.42" />
          <rect x="52" y="27" width="76" height="3" rx="1.5" fill="currentColor" fillOpacity="0.20" />
          <rect x="52" y="36" width="216" height="12" rx="2.5" fill="currentColor" fillOpacity="0.04" />
          <rect x="52" y="36" width="2.5" height="12" rx="1" fill="var(--accent)" fillOpacity="0.80" />
          <rect x="61" y="40.5" width="138" height="3" rx="1.5" fill="currentColor" fillOpacity="0.28" />
          <rect x="52" y="54" width="194" height="3" rx="1.5" fill="currentColor" fillOpacity="0.16" />
          <rect x="52" y="61" width="152" height="3" rx="1.5" fill="currentColor" fillOpacity="0.14" />
        </svg>
      );
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
  const [spFolders, setSpFolders] = useState<SharePointFolderInfo[]>(FALLBACK_SHAREPOINT_FOLDERS);
  const [pinnedIds, setPinnedIds] = useState<Set<string>>(() => new Set());
  const [uploadTarget, setUploadTarget] = useState<GalleryArtifact | null>(null);

  // Prompt-first studio composer state
  const [selectedFormatId, setSelectedFormatId] = useState<StudioFormatId>("sheets");
  const [promptText, setPromptText] = useState("");
  const [domain, setDomain] = useState<PetraDomain>("Operations");
  const [domainTouched, setDomainTouched] = useState(false);
  const [sharePointEnabled, setSharePointEnabled] = useState(false);
  const [sharePointFolder, setSharePointFolder] = useState("Shared Documents/Operations");
  const [sharePointCreateFolder, setSharePointCreateFolder] = useState(true);

  const selectedFormat = useMemo(
    () => STUDIO_FORMATS.find((f) => f.id === selectedFormatId) ?? STUDIO_FORMATS[0],
    [selectedFormatId],
  );

  useEffect(() => {
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
      getSharePointFolders(ctl.signal),
    ])
      .then(([chatsRes, insightsRes, jobsRes, spRes]) => {
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
        if (spRes.status === "fulfilled" && Array.isArray(spRes.value?.folders) && spRes.value.folders.length > 0) {
          setSpFolders(spRes.value.folders);
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

  const selectFormatCard = useCallback(
    (fmt: StudioFormatOption, focusComposer = false) => {
      setSelectedFormatId(fmt.id);
      if (!domainTouched) {
        setDomain(fmt.defaultDomain);
        if (sharePointFolder.startsWith("Shared Documents/")) {
          setSharePointFolder(`Shared Documents/${fmt.defaultDomain}`);
        }
      }
      if (focusComposer) {
        promptInputRef.current?.focus();
      }
    },
    [domainTouched, sharePointFolder],
  );

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
        sharePointEnabled
          ? { enabled: true, folder: sharePointFolder, createIfMissing: sharePointCreateFolder }
          : undefined,
      );
      const href = `/ask?q=${encodeURIComponent(prompt)}`;
      setPromptText("");
      router.push(href);
    },
    [domain, promptText, router, selectedFormat, sharePointCreateFolder, sharePointEnabled, sharePointFolder],
  );

  const hiddenThreadCount = useMemo(
    () => chats.filter((c) => !isDeliverableChat(c)).length,
    [chats],
  );

  const allArtifacts = useMemo<GalleryArtifact[]>(() => {
    const items: GalleryArtifact[] = [];

    // 1. Saved/pinned Insights charts ONLY (never dump unsaved catalog templates)
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

    // 2. Assist jobs ONLY if they produced real prepared deliverables/pieces
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

    // 3. Ask chats ONLY when they represent real files/reports/charts, unless user opts into all threads
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
        title: cleanChatTitle(chat.title || "Untitled thread", Boolean(chat.named)),
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
  }, [charts, chats, includeAskThreads, jobs, pinnedIds]);

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
        {/* Editorial Visual Showcase + Prompt-First "Create a deliverable" Studio */}
        <section
          aria-labelledby="create-deliverable-heading"
          data-testid="create-deliverable-studio"
          className="space-y-4"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2
              id="create-deliverable-heading"
              className="text-[18px] font-semibold tracking-[-0.02em] text-fg"
            >
              Create a deliverable
            </h2>
            <span className="text-[12.5px] text-faint">
              Pick a format or describe any custom workbook, brief, deck, quote, or export
            </span>
          </div>

          {/* 5 Bespoke Editorial Dark-Mode Document Preview Cards */}
          <div
            role="radiogroup"
            aria-label="Deliverable format showcase"
            data-testid="deliverable-showcase"
            className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5"
          >
            {STUDIO_FORMATS.map((fmt) => {
              const active = fmt.id === selectedFormatId;
              return (
                <button
                  key={fmt.id}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  data-selected={active ? "true" : "false"}
                  data-testid={`showcase-card-${fmt.id}`}
                  onClick={() => selectFormatCard(fmt, true)}
                  className={cn(
                    "group relative flex cursor-pointer flex-col overflow-hidden rounded-xl border text-left transition-[border-color,background-color,box-shadow] duration-150",
                    "bg-[#141519] text-[#F1F5F9]",
                    active
                      ? "liquid-glass-active border-white/25 shadow-[0_10px_28px_-10px_rgba(0,0,0,0.65),inset_0_1px_0.5px_rgba(255,255,255,0.38)] ring-1 ring-white/20"
                      : "border-white/10 hover:border-white/22 hover:shadow-[0_10px_22px_-12px_rgba(0,0,0,0.55)]",
                  )}
                >
                  {/* Specular Liquid Glass Top Edge Catchlight */}
                  <span
                    aria-hidden="true"
                    className={cn(
                      "pointer-events-none absolute inset-x-0 top-0 h-px transition-opacity",
                      active
                        ? "bg-white/30 opacity-100"
                        : "bg-white/15 opacity-30 group-hover:opacity-70",
                    )}
                  />

                  <div className="aspect-[220/124] w-full overflow-hidden border-b border-white/[0.07] bg-[#141519] p-1.5">
                    <div className="h-full w-full transition-opacity duration-200 group-hover:opacity-95">
                      <DeliverableArtwork id={fmt.id} active={active} />
                    </div>
                  </div>

                  <div className="flex flex-1 flex-col justify-between gap-1 px-3 py-2.5">
                    <div className="flex items-center justify-between gap-1.5">
                      <span className="truncate text-[13px] font-semibold tracking-[-0.01em] text-[#F8FAFC]">
                        {fmt.title}
                      </span>
                      <span className="shrink-0 rounded bg-white/[0.07] px-1.5 py-0.5 font-mono text-[10px] font-medium text-[#CBD5E1]">
                        {fmt.extension}
                      </span>
                    </div>
                    <p className="line-clamp-2 text-[11.5px] leading-[1.45] text-[#94A3B8]">
                      {fmt.subtitle}
                    </p>
                  </div>
                </button>
              );
            })}
          </div>

          {/* Sleek Elevated Studio Dock: 5 preview cards above own format selection */}
          <form
            onSubmit={submitCreate}
            data-testid="studio-composer-dock"
            className="rounded-xl border border-border/80 bg-surface dark:bg-[#14161b] p-4 shadow-sm transition-colors duration-150 focus-within:border-border-strong"
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
              placeholder={selectedFormat.placeholder}
              data-testid="artifact-prompt-input"
              className="w-full resize-none bg-transparent text-[14.5px] leading-[1.6] text-fg placeholder:text-faint focus:outline-none"
            />

            {/* Single Clean Bottom Toolbar: Active format indicator + Domain + SharePoint + Create */}
            <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-border/70 pt-3">
              <div
                className="inline-flex items-center gap-2 text-[12.5px] text-muted"
                data-testid="studio-active-format"
              >
                <FormatIcon id={selectedFormat.id} className="size-3.5 text-fg" />
                <span className="font-medium text-fg">{selectedFormat.title}</span>
                <span className="font-mono text-[11px] text-faint">{selectedFormat.extension}</span>
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
                      setDomainTouched(true);
                      if (sharePointFolder.startsWith("Shared Documents/")) {
                        setSharePointFolder(`Shared Documents/${next}`);
                      }
                    }}
                    data-testid="artifact-domain-select"
                    className="h-8 cursor-pointer rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-fg transition-colors duration-150 hover:border-border-strong focus:border-accent/60 focus:outline-none"
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
                    "inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-[12px] font-medium transition-colors duration-150 cursor-pointer",
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
              <div className="mt-3 space-y-2.5 border-t border-border/70 pt-3 text-[12.5px]">
                <div className="flex flex-wrap items-center gap-2">
                  <label htmlFor="artifact-sp-folder" className="shrink-0 font-medium text-muted">
                    SharePoint folder
                  </label>
                  <input
                    id="artifact-sp-folder"
                    list="artifact-sp-folder-list"
                    type="text"
                    value={sharePointFolder}
                    onChange={(e) => setSharePointFolder(e.target.value)}
                    placeholder="Shared Documents/Operations"
                    data-testid="artifact-sharepoint-folder"
                    className="h-8 min-w-[220px] flex-1 rounded-md border border-border-strong bg-bg px-2.5 font-mono text-[12px] text-fg placeholder:text-faint focus:border-accent/60 focus:outline-none"
                  />
                  <datalist id="artifact-sp-folder-list">
                    {spFolders.map((f) => (
                      <option key={f.path} value={f.path} />
                    ))}
                  </datalist>
                  <label className="inline-flex cursor-pointer items-center gap-1.5 text-[12px] text-muted select-none hover:text-fg">
                    <input
                      type="checkbox"
                      checked={sharePointCreateFolder}
                      onChange={(e) => setSharePointCreateFolder(e.target.checked)}
                      data-testid="artifact-sharepoint-create-folder"
                      className="size-3.5 cursor-pointer rounded border-border-strong accent-[var(--accent)]"
                    />
                    <FolderPlus className="size-3.5" aria-hidden />
                    <span>Create if missing</span>
                  </label>
                </div>

                {spFolders.length > 0 && (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px]" data-testid="artifact-sp-folder-chips">
                    <span className="text-faint">Known folders:</span>
                    {spFolders.slice(0, 6).map((f) => {
                      const active = f.path.toLowerCase() === sharePointFolder.trim().toLowerCase();
                      return (
                        <button
                          key={f.path}
                          type="button"
                          onClick={() => setSharePointFolder(f.path)}
                          className={cn(
                            "cursor-pointer font-mono text-[11.5px] underline-offset-4 transition-colors duration-150",
                            active
                              ? "font-medium text-fg underline"
                              : "text-muted hover:text-fg hover:underline",
                          )}
                        >
                          {f.path}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </form>

          {/* Clean unboxed quick-start prompt links */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 pt-0.5 text-[12.5px]" data-testid="studio-starter-links">
            <span className="text-[12px] text-faint">Quick start:</span>
            {STARTER_PROMPTS.map((starter) => (
              <button
                key={starter.label}
                type="button"
                onClick={() => {
                  setSelectedFormatId(starter.format);
                  setDomain(starter.domain);
                  setDomainTouched(true);
                  setPromptText(starter.prompt);
                  if (sharePointFolder.startsWith("Shared Documents/")) {
                    setSharePointFolder(`Shared Documents/${starter.domain}`);
                  }
                  promptInputRef.current?.focus();
                }}
                className="cursor-pointer text-left text-[12.5px] text-muted underline-offset-4 transition-colors duration-150 hover:text-fg hover:underline"
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
                      "inline-flex h-7 items-center rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
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
                <div key={idx} className="overflow-hidden rounded-xl border border-border/80 bg-surface dark:bg-[#14161b]">
                  <div className="h-[76px] border-b border-border/60 bg-bg-subtle dark:bg-[#111317] p-3">
                    <div className="h-full w-full rounded bg-surface-2/60 skeleton-shimmer" />
                  </div>
                  <div className="space-y-2.5 p-4">
                    <div className="h-3 w-16 rounded bg-surface-2 skeleton-shimmer" />
                    <div className="h-4 w-3/4 rounded bg-surface-2 skeleton-shimmer" />
                    <div className="h-3 w-1/2 rounded bg-surface-2 skeleton-shimmer" />
                  </div>
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
                  : "Pick a format card or describe an Excel tracker, Word brief, slide deck, or quote above to generate your first deliverable."}
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
                  className="group flex flex-col overflow-hidden rounded-xl border border-border/80 bg-surface dark:bg-[#14161b] transition-[border-color] duration-150 hover:border-border-strong"
                >
                  {/* Visual Format Preview Header Strip */}
                  <div className="relative h-[76px] w-full overflow-hidden border-b border-border/70 bg-bg-subtle dark:bg-[#111317] text-fg">
                    <DeliverableShelfPreview type={item.type} />
                    <button
                      type="button"
                      onClick={() => togglePin(item.id)}
                      aria-label={item.pinned ? `Unpin ${item.title}` : `Pin ${item.title}`}
                      title={item.pinned ? "Unpin" : "Pin"}
                      className={cn(
                        "absolute top-2 right-2 flex size-6 items-center justify-center rounded-md border border-transparent transition-colors duration-150 cursor-pointer",
                        item.pinned
                          ? "bg-surface/90 text-fg border-border/80 hover:bg-hover"
                          : "text-faint opacity-75 hover:bg-surface/80 hover:text-fg hover:opacity-100",
                      )}
                    >
                      {item.pinned ? <PinOff className="size-3.5" aria-hidden /> : <Pin className="size-3.5" aria-hidden />}
                    </button>
                  </div>

                  {/* Card Body + 1-Click SharePoint & Open Footer */}
                  <div className="flex flex-1 flex-col justify-between gap-3.5 p-4">
                    <div className="space-y-1.5">
                      <div className="flex items-center justify-between gap-2 text-[11.5px]">
                        <span className="inline-flex items-center gap-1.5 font-medium text-muted">
                          <ArtifactTypeIcon type={item.type} className="size-3.5 text-muted" />
                          <span>{item.type}</span>
                        </span>
                        <span className="num text-faint">
                          {item.date ? formatDate(item.date, { short: true }) : "Saved"}
                        </span>
                      </div>

                      <h3 className="line-clamp-2 text-[14.5px] leading-[1.38] font-semibold tracking-[-0.015em] text-fg">
                        {item.title}
                      </h3>

                      {item.subtitleParts.length > 0 && (
                        <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] leading-[18px] text-muted">
                          {item.subtitleParts.map((part, idx) => (
                            <span key={`${part}-${idx}`} className="inline-flex items-center gap-2">
                              {idx > 0 && <span className="h-2.5 w-px bg-border-strong" aria-hidden />}
                              <span>{part}</span>
                            </span>
                          ))}
                        </p>
                      )}
                    </div>

                    <div className="flex items-center justify-between gap-2 border-t border-border/70 pt-2.5">
                      <button
                        type="button"
                        onClick={() => setUploadTarget(item)}
                        data-testid="artifact-save-sharepoint"
                        aria-label={`Save ${item.title} to SharePoint`}
                        title="Save to SharePoint"
                        className="inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md border border-border px-2.5 text-[12px] font-medium text-muted transition-colors duration-150 hover:border-border-strong hover:bg-surface-2 hover:text-fg"
                      >
                        <FolderUp className="size-3.5" aria-hidden />
                        <span>SharePoint</span>
                      </button>

                      <Link
                        href={item.href}
                        aria-label={`${item.openLabel}: ${item.title}`}
                        className="inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-[12px] font-medium text-fg transition-colors duration-150 hover:bg-surface-2"
                      >
                        <span>Open</span>
                        <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
                      </Link>
                    </div>
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
                  <button
                    type="button"
                    onClick={() => setUploadTarget(item)}
                    data-testid="artifact-save-sharepoint"
                    aria-label={`Save ${item.title} to SharePoint`}
                    title="Save to SharePoint"
                    className="inline-flex h-7 cursor-pointer items-center gap-1 rounded-md border border-border px-2 text-[11.5px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-surface-2 hover:text-fg"
                  >
                    <FolderUp className="size-3" aria-hidden />
                    <span>SharePoint</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => togglePin(item.id)}
                    aria-label={item.pinned ? `Unpin ${item.title}` : `Pin ${item.title}`}
                    title={item.pinned ? "Unpin" : "Pin"}
                    className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
                  >
                    {item.pinned ? <PinOff className="size-3.5" aria-hidden /> : <Pin className="size-3.5" aria-hidden />}
                  </button>
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

      {uploadTarget && (
        <SharePointUploadPopover
          artifact={uploadTarget}
          folders={spFolders}
          onClose={() => setUploadTarget(null)}
        />
      )}
    </div>
  );
}
