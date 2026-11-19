"use client";

// One panel, used everywhere a record, a person or an autopilot item is shown: what your
// memory keeps about it, the templates that fit it, your work log on it and, for the owner,
// the autopilot's run log for the item. Each block links to its own page, so memory,
// templates and logs are a click away from any section. Every read is the reader's own
// (memory and the work log are never anyone else's; the run log is the owner's only), and
// viewing as someone reads none of their memory, as the server refuses it too.

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useAuth } from "@/components/auth";
import { TemplateFilledView, DOC_TYPE_LABEL, refusal } from "@/components/templates";
import { useViewingAs } from "@/components/view-as";
import { continueHref, recordName, verbLabel } from "@/components/work-log";
import { fillTemplate, getAutopilotAudit, getMemory, getTemplates, getWorkLog } from "@/lib/api";
import { auditWhat } from "@/lib/autopilot";
import type { DraftUseData } from "@/components/autopilot/draft-use";
import { cn } from "@/lib/cn";
import { formatDateTime, plural } from "@/lib/format";
import type { AutopilotAuditRow, MemoryItem, Template, TemplateDocType, TemplateFilled, WorkLogEvent } from "@/lib/types";

/** What the panel is about: records (object ids), and for an autopilot item its id and workflow. */
export interface RelatedProps {
  records: string[];
  /** The record's name in words, for "Fill for …"; the first record's id when absent. */
  label?: string | null;
  /** Template types that fit what is being drafted (an autopilot reply, a quote …). */
  docTypes?: TemplateDocType[];
  itemId?: string | null;
  workflow?: string | null;
  /** The narrow column of a detail page or answer: smaller rows, no work-log actions. */
  dense?: boolean;
  /** An autopilot draft's own provenance (DraftUse shows it above the panel): the memory and
   * template it lists are left out here, and what is left is labelled as not applied. */
  used?: DraftUseData | null;
  className?: string;
}

type Read<T> = { rows: T[]; failed: boolean } | null;

const MEMORY_KIND: Record<string, string> = {
  instruction: "Instruction",
  preference: "Preference",
  commitment: "Commitment",
  fact: "Fact",
  summary: "Summary",
};

const MAX_RECORDS = 4;
const SHOWN = 5;

/** The records an autopilot item is about: what happened's own record, then the ones it names. */
export function itemRecords(happened: Record<string, unknown> | null | undefined): string[] {
  const h = happened ?? {};
  const out: string[] = [];
  const add = (v: unknown) => {
    if (typeof v === "string" && v.includes(":") && !out.includes(v)) out.push(v);
  };
  add(h.object_id);
  if (Array.isArray(h.records)) h.records.forEach(add);
  return out;
}

/** The template types an autopilot item's draft fits, from its system and the side it is on. */
export function itemDocTypes(system: string | null | undefined, happened: Record<string, unknown> | null | undefined): TemplateDocType[] {
  const side = typeof happened?.side === "string" ? happened.side : "";
  if (system === "mail" || system === "teams") {
    if (side === "supplier") return ["supplier_email", "phrasing"];
    if (side === "customer") return ["customer_email", "order_confirmation", "phrasing"];
    return ["customer_email", "supplier_email", "phrasing"];
  }
  if (system === "portal" || system === "bc") return ["order_confirmation", "quote"];
  return [];
}

/** The memory ids and template id a draft's provenance already lists. */
export function usedIds(used: DraftUseData | null | undefined): { memory: Set<number>; template: number | null } {
  const memory = new Set<number>([...(used?.memory ?? []), ...(used?.related ?? [])].map((e) => e.id));
  return { memory, template: used?.template?.id ?? null };
}

/** Memory items about any of the records, newest first. */
export function memoryFor(items: MemoryItem[], records: string[]): MemoryItem[] {
  const want = new Set(records);
  return items.filter((m) => (m.records ?? []).some((r) => want.has(r))).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

/** Templates made from one of the records first, then those of a fitting type by use. */
export function templatesFor(list: Template[], records: string[], docTypes: TemplateDocType[]): Template[] {
  const want = new Set(records);
  const fromRecord = list.filter((t) => t.source_ref && want.has(t.source_ref));
  const ofType = list.filter((t) => !fromRecord.includes(t) && docTypes.includes(t.doc_type as TemplateDocType));
  ofType.sort((a, b) => b.uses - a.uses || Date.parse(b.last_used_at ?? b.updated_at) - Date.parse(a.last_used_at ?? a.updated_at));
  return [...fromRecord, ...ofType];
}

/** The audit rows of one item, or of a workflow when no item is named, newest first. */
export function runLogFor(rows: AutopilotAuditRow[], itemId?: string | null, workflow?: string | null): AutopilotAuditRow[] {
  if (itemId) return rows.filter((r) => r.item_id === itemId);
  if (workflow) return rows.filter((r) => r.workflow === workflow);
  return [];
}

// One read of memory, templates and the audit log serves every panel on a screen (a chat of
// ten answers, a detail pane and its record) for a few seconds; keyed by who is signed in.
const SHARED_MS = 15_000;
const shared = new Map<string, { at: number; p: Promise<unknown> }>();
function once<T>(key: string, read: () => Promise<T>): Promise<T> {
  const hit = shared.get(key);
  if (hit && Date.now() - hit.at < SHARED_MS) return hit.p as Promise<T>;
  const p = read();
  shared.set(key, { at: Date.now(), p });
  p.catch(() => shared.delete(key));
  return p;
}
/** Tests and a save elsewhere start from a fresh read. */
export function forgetRelatedReads() {
  shared.clear();
}

function settle<T>(p: Promise<T[]>, set: (r: Read<T>) => void, signal: AbortSignal) {
  p.then(
    (rows) => !signal.aborted && set({ rows, failed: false }),
    () => !signal.aborted && set({ rows: [], failed: true }),
  );
}

function Block({ title, href, linkText, count, children, testId }: { title: string; href: string; linkText: string; count?: number; children: React.ReactNode; testId: string }) {
  return (
    <div data-testid={testId}>
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[12px] font-medium text-muted">
          {title}
          {count ? <span className="ml-1.5 tabular-nums text-faint">{count}</span> : null}
        </h3>
        <Link href={href} className="text-[12px] text-muted hover:text-fg hover:underline">
          {linkText}
        </Link>
      </div>
      <ul className="mt-1 space-y-1">{children}</ul>
    </div>
  );
}

function TemplateRow({ t, record, label, dense }: { t: Template; record: string | null; label: string; dense?: boolean }) {
  const [filled, setFilled] = useState<TemplateFilled | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fill = async () => {
    if (!record || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      setFilled(await fillTemplate(t.id, record));
    } catch (err) {
      setProblem(refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="text-[13px] leading-5" data-testid="related-template">
      <div className={cn("flex gap-x-2", dense ? "flex-col" : "flex-wrap items-baseline")}>
        <span className="min-w-0 truncate font-medium">{t.name}</span>
        <span className="text-[12px] text-muted">
          {DOC_TYPE_LABEL[t.doc_type as TemplateDocType] ?? t.doc_type}
          {t.mine ? "" : ", shared with you"}
          {t.uses ? `, used ${plural(t.uses, "time")}` : ""}
        </span>
        {record && !filled && (
          <button type="button" onClick={fill} disabled={busy} className="cursor-pointer text-left text-[12px] font-medium text-[color:var(--accent)] hover:underline disabled:opacity-60" data-testid="related-template-fill">
            {busy ? "Filling" : `Fill for ${label}`}
          </button>
        )}
      </div>
      {problem && <p className="text-[12px] text-[color:var(--late)]">{problem}</p>}
      {filled && (
        <div className="mt-2">
          <TemplateFilledView filled={filled} />
        </div>
      )}
    </li>
  );
}

export function Related({ records, label, docTypes = [], itemId, workflow, dense, className, used }: RelatedProps) {
  const viewing = useViewingAs();
  const { state } = useAuth();
  const owner = state.status === "signed-in" && !!state.me.owner;
  const who = state.status === "signed-in" ? state.me.principal : "";
  const recs = useMemo(() => [...new Set(records.filter(Boolean))].slice(0, MAX_RECORDS), [records]);
  const recKey = recs.join("|");
  const typeKey = docTypes.join("|");
  const [memory, setMemory] = useState<Read<MemoryItem>>(null);
  const [templates, setTemplates] = useState<Read<Template>>(null);
  const [log, setLog] = useState<Read<WorkLogEvent>>(null);
  const [runs, setRuns] = useState<Read<AutopilotAuditRow>>(null);

  useEffect(() => {
    const c = new AbortController();
    const ids = recKey ? recKey.split("|") : [];
    const types = (typeKey ? typeKey.split("|") : []) as TemplateDocType[];
    setMemory(null);
    setTemplates(null);
    setLog(null);
    setRuns(null);
    // viewing as someone: none of their memory, templates or log (the server refuses too)
    if (viewing) return () => c.abort();
    if (ids.length) {
      settle(once(`memory:${who}`, () => getMemory()).then((m) => (m.available ? memoryFor(m.items ?? [], ids) : Promise.reject(new Error("unavailable")))), setMemory, c.signal);
      settle(
        Promise.all(ids.map((r) => getWorkLog({ record: r, limit: SHOWN }, c.signal))).then((pages) => {
          const seen = new Map<number, WorkLogEvent>();
          for (const p of pages) for (const e of p.events) if (!seen.has(e.seq)) seen.set(e.seq, e);
          return [...seen.values()].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
        }),
        setLog,
        c.signal,
      );
    }
    if (ids.length || types.length)
      settle(once(`templates:${who}`, () => getTemplates()).then((l) => (l.available ? templatesFor(l.templates ?? [], ids, types) : [])), setTemplates, c.signal);
    if (owner && (itemId || workflow)) settle(once(`audit:${who}`, () => getAutopilotAudit()).then((a) => runLogFor(a.rows ?? [], itemId, workflow)), setRuns, c.signal);
    return () => c.abort();
  }, [recKey, typeKey, itemId, workflow, owner, viewing, who]);

  if (viewing) return null;
  const first = recs[0] ?? null;
  const drafted = !!used && (!!used.memory?.length || !!used.related?.length || !!used.template);
  const listed = usedIds(used);
  const memoryRows = (memory?.rows ?? []).filter((m) => !listed.memory.has(m.id));
  const templateRows = (templates?.rows ?? []).filter((t) => t.id !== listed.template);
  const name = label?.trim() || (first ? recordName({ object_id: first }) : "");
  const loading = (recs.length > 0 && (!memory || !log)) || ((recs.length > 0 || docTypes.length > 0) && !templates) || (owner && !!(itemId || workflow) && !runs);
  const empty = !loading && !memoryRows.length && !templateRows.length && !log?.rows.length && !runs?.rows.length;
  const failed = [memory?.failed && "memory", templates?.failed && "templates", log?.failed && "work log", runs?.failed && "run log"].filter(Boolean) as string[];
  const memoryHref = first ? `/memory?record=${encodeURIComponent(first)}` : "/memory";
  const logHref = first ? `/work-log?record=${encodeURIComponent(first)}` : "/work-log";
  return (
    <section aria-label="Related" className={cn("space-y-4", className)} data-testid="related-panel">
      <h2 className="text-[13px] font-semibold">Related</h2>
      {loading && <p className="text-[12px] text-muted">Reading your memory, templates and work log</p>}
      {memoryRows.length > 0 && (
        <Block title={drafted ? "Memory, not applied" : "Memory"} href={memoryHref} linkText="Memory" count={memoryRows.length} testId="related-memory">
          {memoryRows.slice(0, SHOWN).map((m) => (
            <li key={m.id} className="text-[13px] leading-5" data-testid="related-memory-row">
              <span className="text-muted">{MEMORY_KIND[m.kind] ?? m.kind}: </span>
              {m.text}
              {m.due && <span className="text-muted"> (due {formatDateTime(m.due)})</span>}
            </li>
          ))}
        </Block>
      )}
      {templateRows.length > 0 && (
        <Block title={drafted ? "Templates, not applied" : "Templates"} href="/templates" linkText="Templates" count={templateRows.length} testId="related-templates">
          {templateRows.slice(0, SHOWN).map((t) => (
            <TemplateRow key={t.id} t={t} record={first} label={name} dense={dense} />
          ))}
        </Block>
      )}
      {!!log?.rows.length && (
        <Block title="Your work log" href={logHref} linkText="Work log" count={log.rows.length} testId="related-work-log">
          {log.rows.slice(0, SHOWN).map((e) => (
            <li key={e.seq} className={cn("flex gap-x-2 text-[13px] leading-5", dense ? "flex-col" : "items-baseline justify-between")} data-testid="related-log-row">
              {/* wraps, never truncates: a clipped line still runs its link off a phone's screen */}
              <span className="min-w-0 line-clamp-3 [overflow-wrap:anywhere]" data-testid="related-log-text">
                <span className="text-muted">{verbLabel(e)}: </span>
                {e.mine ? (
                  <Link href={continueHref(e.thread)} className="hover:underline">
                    {e.thread_name || e.title}
                  </Link>
                ) : (
                  e.thread_name || e.title
                )}
              </span>
              <span className="shrink-0 text-[12px] tabular-nums text-muted">{formatDateTime(e.at)}</span>
            </li>
          ))}
        </Block>
      )}
      {!!runs?.rows.length && (
        <Block title={itemId ? "Run log for this item" : "Run log"} href="/autopilot-log" linkText="Audit log" count={runs.rows.length} testId="related-run-log">
          {runs.rows.slice(0, SHOWN).map((r, i) => (
            <li key={`${r.at}-${r.event}-${i}`} className={cn("flex gap-x-2 text-[13px] leading-5", dense ? "flex-col" : "items-baseline justify-between")} data-testid="related-run-row">
              <span className="min-w-0">
                {auditWhat(r)}
                {r.mode && r.mode !== "live" ? <span className="text-muted"> ({r.mode.replace(/_/g, " ")})</span> : null}
                {r.by ? <span className="text-muted"> by {r.by}</span> : null}
              </span>
              <span className="shrink-0 text-[12px] tabular-nums text-muted">{formatDateTime(r.at)}</span>
            </li>
          ))}
        </Block>
      )}
      {empty && (
        <p className="text-[13px] leading-5 text-muted" data-testid="related-empty">
          {drafted && (memory?.rows.length || templates?.rows.length)
            ? "Nothing else in your memory, templates or work log on this."
            : recs.length
              ? "Your memory, templates and work log hold nothing on this yet."
              : "No templates of this kind yet."}
        </p>
      )}
      {failed.length > 0 && (
        <p className="text-[12px] text-muted" data-testid="related-failed">
          Couldn&apos;t read your {failed.join(" or ")} just now.
        </p>
      )}
      {empty && (
        <p className="flex flex-wrap gap-x-3 text-[12px]" data-testid="related-links">
          <Link href={memoryHref} className="text-muted hover:text-fg hover:underline">
            Memory
          </Link>
          <Link href="/templates" className="text-muted hover:text-fg hover:underline">
            Templates
          </Link>
          <Link href={logHref} className="text-muted hover:text-fg hover:underline">
            Work log
          </Link>
          {owner && (itemId || workflow) && (
            <Link href="/autopilot-log" className="text-muted hover:text-fg hover:underline">
              Audit log
            </Link>
          )}
        </p>
      )}
    </section>
  );
}
