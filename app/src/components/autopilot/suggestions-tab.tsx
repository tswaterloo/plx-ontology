"use client";

import {
  AlertTriangle,
  ArrowUpRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  Copy,
  FileText,
  Mail,
  RefreshCw,
  Repeat,
  ShieldCheck,
  Sparkles,
  UserRound,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { collapsedLine, draftText, etaBadge, suggestionHeadline } from "@/components/proactive-suggestions";
import { SourceChip } from "@/components/source-chip";
import { Button } from "@/components/ui/button";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { WritePanel } from "@/components/write-panel";
import { actionChip, isPutAway } from "@/lib/actions";
import {
  createAssistWorkflow,
  getActionStates,
  getAssistWorkflows,
  getSuggestions,
  type AssistMinedRoutine,
} from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatMoney, objectHref, plural, readable, safeHref } from "@/lib/format";
import type { Status } from "@/lib/status";
import type { ActionResult, ProactiveSuggestion, SuggestionDraft, Suggestions } from "@/lib/types";

const SERVER_NOTE =
  "Nothing is sent until you review a draft and confirm it yourself; it then goes from your own mailbox, or is recorded as a dry run while sending is not enabled.";

function workflowPromptFor(s: ProactiveSuggestion): string {
  const head = suggestionHeadline(s);
  const act = (s.action?.text ?? "").trim();
  if (act) return `Every Monday at 8:00 AM, check for items like "${head}" and prepare: ${act}`;
  return `Every Monday at 8:00 AM, monitor for items like "${head}" and draft the follow-up.`;
}

function statusTextClass(status: Status): string {
  switch (status) {
    case "late":
      return "text-late";
    case "risk":
    case "suspect":
      return "text-risk";
    case "ok":
      return "text-ok";
    case "new":
      return "text-new";
    default:
      return "text-muted";
  }
}

function statusDotClass(status: Status): string {
  switch (status) {
    case "late":
      return "bg-dot-late";
    case "risk":
    case "suspect":
      return "bg-dot-risk";
    case "ok":
      return "bg-dot-ok";
    case "new":
      return "bg-new";
    default:
      return "bg-dot-neutral";
  }
}

function extractDaysLate(headline: string): string | null {
  const m = /\b(\d+)\s+days?\s+late\b/i.exec(headline);
  return m ? `${m[1]}d late` : null;
}

function evidenceBadgeOf(s: ProactiveSuggestion): { label: string; status: Status } {
  if (s.confidence?.level === "high") return { label: "Strong evidence", status: "ok" };
  if (s.confidence?.level === "medium") return { label: "Check before acting", status: "risk" };
  return { label: "Weak evidence", status: "neutral" };
}

function formatDraftTo(to: SuggestionDraft["to"] | string | string[] | null | undefined): string {
  if (!to) return "No address on record";
  if (typeof to === "string") return to.trim() || "No address on record";
  if (Array.isArray(to)) return to.filter(Boolean).join(", ") || "No address on record";
  if (to.address) return to.name ? `${to.name} <${to.address}>` : to.address;
  return to.name || "No address on record";
}

function DraftPreview({ draft }: { draft: SuggestionDraft }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(draftText(draft)).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      },
      () => undefined,
    );
  };
  const title =
    draft.audience === "vendor"
      ? "Draft to the supplier"
      : draft.audience === "customer"
        ? "Draft to the customer"
        : "Internal note";
  const toLabel = formatDraftTo(draft?.to as SuggestionDraft["to"] | string | string[] | null | undefined);

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-bg" data-testid="suggestion-draft">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-bg-subtle px-3.5 py-2">
        <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-fg">
          <Mail className="size-3.5 text-muted" aria-hidden />
          {title}
          <span className="font-normal text-faint">(not sent)</span>
        </span>
        <button
          type="button"
          onClick={copy}
          data-testid="copy-draft"
          className="inline-flex h-6 items-center gap-1 rounded-md border border-border bg-surface px-2 text-[11.5px] font-medium text-muted hover:border-border-strong hover:text-fg cursor-pointer"
        >
          {copied ? <Check className="size-3 text-ok" aria-hidden /> : <Copy className="size-3" aria-hidden />}
          <span>{copied ? "Copied" : "Copy draft"}</span>
        </button>
      </div>
      <div className="space-y-1.5 border-b border-border px-3.5 py-2.5 text-[12.5px]">
        <div className="flex items-baseline gap-3">
          <span className="w-12 shrink-0 text-faint">To</span>
          <span className="min-w-0 flex-1 break-words text-fg" data-testid="draft-to">
            {toLabel}
          </span>
        </div>
        <div className="flex items-baseline gap-3">
          <span className="w-12 shrink-0 text-faint">Subject</span>
          <span className="min-w-0 flex-1 font-medium text-fg">{draft.subject ?? ""}</span>
        </div>
      </div>
      <pre className="max-h-[220px] overflow-y-auto px-3.5 py-3 font-sans text-[13px] leading-[1.6] break-words whitespace-pre-wrap text-fg/95">
        {draft.body ?? ""}
      </pre>
      {(draft.facts ?? []).length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-border bg-bg-subtle px-3.5 py-2 text-[11.5px]">
          <span className="text-faint">Figures from</span>
          {(draft.facts ?? []).slice(0, 5).map((f, n) =>
            f?.source ? (
              <SourceChip key={`${f.source.handle ?? "src"}-${n}`} source={f.source} record={readable(f.value ?? "")} />
            ) : null,
          )}
        </div>
      )}
    </div>
  );
}

function SuggestionActionCard({
  s,
  defaultDraftOpen,
  state,
  onDone,
  workflowId,
  creating,
  creatingAny,
  onMakeWorkflow,
}: {
  s: ProactiveSuggestion;
  defaultDraftOpen: boolean;
  state?: ActionResult | null;
  onDone: (r: ActionResult) => void;
  workflowId?: string;
  creating: boolean;
  creatingAny: boolean;
  onMakeWorkflow: () => void;
}) {
  const [draftOpen, setDraftOpen] = useState(defaultDraftOpen);
  const [evidenceOpen, setEvidenceOpen] = useState(false);

  const headline = suggestionHeadline(s);
  const href =
    safeHref(s.subject?.open_url) ??
    (s.subject?.type && s.subject?.object_id ? objectHref(s.subject.type, s.subject.object_id) : "/records");
  const eta = etaBadge(s.eta);
  const lateTag = extractDaysLate(headline);
  const evidence = evidenceBadgeOf(s);
  const chip = actionChip(state, !!s.draft);
  const amount =
    s.impact?.amount && !s.impact.amount_suspect
      ? formatMoney(s.impact.amount.amount, s.impact.amount.currency)
      : null;
  const rawTo = s.draft?.to as SuggestionDraft["to"] | string | string[] | null | undefined;
  const contactLabel = !rawTo
    ? null
    : typeof rawTo === "string"
      ? rawTo.trim() || null
      : Array.isArray(rawTo)
        ? rawTo.filter(Boolean).join(", ") || null
        : rawTo.name || rawTo.address || null;
  const impactOrders = s.impact?.orders ?? [];
  const etaWords = s.eta ? readable(s.eta.text).trim() : "";
  const reasons = (s.reasoning ?? []).filter((r) => !etaWords || !readable(r.text).includes(etaWords));
  const hasEvidence = Boolean(s.eta || reasons.length > 0 || (s.missing ?? []).length > 0 || s.confidence?.why);

  return (
    <article
      data-testid="suggestion"
      className="py-5 transition-colors"
    >
      {/* Header row: entity title + quiet typographic metadata */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <Link
              href={href}
              data-testid="suggestion-headline"
              className="text-[15px] font-semibold tracking-[-0.01em] text-fg hover:text-accent hover:underline"
            >
              {headline}
            </Link>
            {lateTag && (
              <span className="num text-[12px] font-medium text-late">
                {lateTag}
              </span>
            )}
            {eta && (
              <span
                data-testid="suggestion-eta"
                className={cn(
                  "text-[12px] font-medium",
                  eta.status === "late" ? "text-late" : eta.status === "ok" ? "text-ok" : eta.status === "risk" ? "text-risk" : "text-muted",
                )}
              >
                {eta.label}
              </span>
            )}
            <span
              className={cn(
                "text-[12px] font-medium",
                evidence.status === "ok" ? "text-fg/75" : evidence.status === "risk" ? "text-risk" : "text-faint",
              )}
            >
              {evidence.label}
            </span>
          </div>

          {s.action?.text && (
            <p className="text-[13.5px] leading-[1.55] text-fg/90">{s.action.text}</p>
          )}
        </div>

        <div className="flex shrink-0 flex-col items-end gap-1">
          {amount && (
            <span className="num text-[13px] font-semibold text-fg">
              {amount}
            </span>
          )}
          {chip && (
            <span
              data-testid="action-state"
              title={chip.title}
              className={cn(
                "inline-flex items-center gap-1.5 text-[12px] font-medium",
                statusTextClass(chip.status),
              )}
            >
              <span className={cn("size-1.5 shrink-0 rounded-full", statusDotClass(chip.status))} aria-hidden />
              <span>{chip.label}</span>
            </span>
          )}
        </div>
      </div>

      {/* Contact + affected downstream orders metadata */}
      {(contactLabel || impactOrders.length > 0) && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-muted">
          {contactLabel && (
            <span className="inline-flex items-center gap-1.5">
              <UserRound className="size-3.5 text-faint" aria-hidden />
              <span className="text-fg/85">{contactLabel}</span>
            </span>
          )}
          {impactOrders.length > 0 && (
            <span className="inline-flex flex-wrap items-center gap-1">
              <span className="text-faint">Holds up</span>
              {impactOrders.map((o, idx) => (
                <span key={o.object_id}>
                  {idx > 0 && ", "}
                  <Link
                    href={safeHref(o.open_url) ?? objectHref(o.type, o.object_id)}
                    className="font-medium text-accent hover:underline"
                  >
                    {o.label}
                  </Link>
                </span>
              ))}
            </span>
          )}
        </div>
      )}

      {/* Collapsible draft and evidence toggles */}
      {(s.draft || hasEvidence) && (
        <div className="mt-2.5 space-y-2.5">
          <div className="flex flex-wrap items-center gap-3 text-[12px]">
            {s.draft && (
              <button
                type="button"
                aria-expanded={draftOpen}
                onClick={() => setDraftOpen((v) => !v)}
                className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                {draftOpen ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
                <FileText className="size-3.5" aria-hidden />
                <span>{draftOpen ? "Hide draft preview" : "Preview draft"}</span>
              </button>
            )}
            {hasEvidence && (
              <button
                type="button"
                aria-expanded={evidenceOpen}
                onClick={() => setEvidenceOpen((v) => !v)}
                data-testid="suggestion-toggle"
                className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                {evidenceOpen ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
                <span>Evidence and checks</span>
              </button>
            )}
          </div>

          {s.draft && draftOpen && <DraftPreview draft={s.draft} />}

          {evidenceOpen && hasEvidence && (
            <div className="space-y-2.5 border-l-2 border-border-strong pl-4 py-1 text-[12.5px]" data-testid="suggestion-detail">
              <WritePanel match={[s.id, s.subject?.object_id ?? ""]} />
              {s.eta && (
                <p className="leading-5 text-fg">
                  <span className="text-faint">Supplier date: </span>
                  {readable(s.eta.text)}
                  {s.eta.source && (
                    <span className="ml-1.5 align-middle">
                      <SourceChip source={s.eta.source} />
                    </span>
                  )}
                </p>
              )}
              {reasons.length > 0 && (
                <ul className="space-y-1 text-fg/90" data-testid="suggestion-reasoning">
                  {reasons.map((r, idx) => (
                    <li key={idx}>
                      {readable(r.text).replace(/^ETA confirmed\? (?:yes|no|possibly|unknown)\.\s*/i, "")}
                      {r.source && (
                        <span className="ml-1.5 align-middle">
                          <SourceChip source={r.source} />
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {s.confidence?.why && (
                <p className="text-muted">
                  <span className="font-medium text-fg">{evidence.label}: </span>
                  {readable(s.confidence.why)}
                </p>
              )}
              {(s.missing ?? []).length > 0 && (
                <div data-testid="suggestion-missing">
                  <p className="mb-1 text-[11.5px] font-medium uppercase tracking-wide text-faint">Not on record</p>
                  <ul className="list-disc space-y-0.5 pl-4 text-muted">
                    {(s.missing ?? []).map((m, idx) => (
                      <li key={idx}>{m}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Clean 1-click action bar */}
      <div className="mt-3.5 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <CardActions card={s} onDone={onDone} state={state} />
          <Link
            href={href}
            className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg"
          >
            <span>Open record</span>
            <ArrowUpRight className="size-3" aria-hidden />
          </Link>
        </div>

        {workflowId ? (
          <span
            className="inline-flex items-center gap-1.5 text-[12px] font-medium text-ok"
            data-testid="suggestion-workflow-created"
          >
            <CheckCircle2 className="size-3.5" aria-hidden />
            Added as workflow
            <Link href="/assist?show=workflows" className="text-accent underline underline-offset-2">
              Open in Workflows
            </Link>
          </span>
        ) : (
          <button
            type="button"
            disabled={creating || creatingAny}
            onClick={onMakeWorkflow}
            data-testid="suggestion-make-workflow"
            className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border-strong px-2.5 text-[12px] font-medium text-fg transition-colors hover:bg-hover disabled:opacity-50 cursor-pointer"
          >
            <Repeat className="size-3.5 text-muted" aria-hidden />
            <span>{creating ? "Saving workflow..." : "Turn into workflow"}</span>
          </button>
        )}
      </div>
    </article>
  );
}

/**
 * Proactive Suggestions tab in /assist:
 * - Surfaces everything the ontology spotted in /api/suggestions as structured actionable cards.
 * - Lets users act directly (Review & send, Snooze, Dismiss) or turn any item into a workflow.
 */
export function AssistSuggestionsView() {
  const [data, setData] = useState<Suggestions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const [putAway, setPutAway] = useState(false);
  const [routines, setRoutines] = useState<AssistMinedRoutine[]>([]);
  const [creatingId, setCreatingId] = useState<string | null>(null);
  const [createdIds, setCreatedIds] = useState<Record<string, string>>({});
  const [workflowError, setWorkflowError] = useState<string | null>(null);

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getSuggestions(c.signal)
      .then(setData)
      .catch((e) => {
        if (!c.signal.aborted) setError(plainError(e));
      });
    getActionStates(c.signal).then((r) => setStates(r.cards ?? {}), () => undefined);
    getAssistWorkflows(c.signal)
      .then((res) => {
        if (!c.signal.aborted) setRoutines(res.routines ?? []);
      })
      .catch(() => undefined);
    return () => c.abort();
  }, []);

  useEffect(() => load(), [load]);

  const onActionDone = useCallback((r: ActionResult) => {
    setStates((prev) => ({ ...prev, [r.card_id]: r }));
  }, []);

  const turnSuggestionIntoWorkflow = async (s: ProactiveSuggestion) => {
    if (creatingId) return;
    setCreatingId(s.id);
    setWorkflowError(null);
    try {
      const wf = await createAssistWorkflow({
        plain_text: workflowPromptFor(s),
        name: suggestionHeadline(s).slice(0, 80),
      });
      setCreatedIds((prev) => ({ ...prev, [s.id]: wf.workflow_id }));
    } catch (e) {
      setWorkflowError(plainError(e));
    } finally {
      setCreatingId(null);
    }
  };

  const turnRoutineIntoWorkflow = async (routine: AssistMinedRoutine) => {
    if (creatingId) return;
    setCreatingId(routine.routine_id);
    setWorkflowError(null);
    try {
      const wf = await createAssistWorkflow({ routine_id: routine.routine_id });
      setCreatedIds((prev) => ({ ...prev, [routine.routine_id]: wf.workflow_id }));
    } catch (e) {
      setWorkflowError(plainError(e));
    } finally {
      setCreatingId(null);
    }
  };

  const every = useMemo(() => data?.items ?? [], [data]);
  const away = useMemo(() => every.filter((s) => isPutAway(states[s.id])), [every, states]);
  const visible = useMemo(
    () => (putAway ? every : every.filter((s) => !isPutAway(states[s.id]))),
    [every, putAway, states],
  );
  const readyDrafts = useMemo(() => visible.filter((s) => !!s.draft).length, [visible]);
  const extra = collapsedLine(data?.collapsed);
  const suggestionsReady = Boolean(
    data && (data.status === "read" || data.status === ("ok" as string) || every.length > 0),
  );

  return (
    <div className="mx-auto w-full max-w-[960px] space-y-6 pb-10" data-testid="assist-suggestions-view">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
        <div className="min-w-0 flex-1 space-y-1.5">
          <h2 className="text-[19px] font-semibold tracking-tight text-fg">
            Proactive suggestions
          </h2>
          <p className="text-[14px] leading-[1.6] text-muted">
            Follow-ups spotted across orders, suppliers, and messages. Review and send a draft here, or turn any item into a scheduled workflow.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2.5 text-[12.5px] text-muted">
          {suggestionsReady && (
            <span className="num inline-flex items-center gap-2 rounded-md border border-border bg-surface px-2.5 py-1 text-fg">
              <span>
                <strong>{visible.length}</strong> open
              </span>
              {readyDrafts > 0 && (
                <>
                  <span className="h-3 w-px bg-border" aria-hidden />
                  <span className="text-muted">{readyDrafts} with drafts</span>
                </>
              )}
            </span>
          )}
          <Link
            href="/assist?show=workflows"
            className="inline-flex h-8 items-center gap-1 rounded-lg border border-border bg-surface px-3 text-[12.5px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2"
          >
            <span>View workflows</span>
            <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
          </Link>
        </div>
      </header>

      <StaleNote since={data?.stale_since} />

      {workflowError && (
        <div role="alert" className="border-l-2 border-l-late pl-3.5 py-1 text-[13px] text-late">
          {workflowError}
        </div>
      )}

      {error ? (
        <LoadProblem message={error} onRetry={load} />
      ) : !data ? (
        <Pending label="Reading proactive suggestions" />
      ) : !suggestionsReady ? (
        <div
          className="rounded-xl border border-border bg-surface px-6 py-12 text-center"
          data-testid="assist-suggestions-not-ready"
        >
          <Sparkles className="mx-auto mb-3 size-5 text-muted" aria-hidden />
          <h3 className="text-[15px] font-semibold tracking-[-0.01em] text-fg">
            Suggestions are not ready yet
          </h3>
          <p className="mx-auto mt-1.5 max-w-[48ch] text-[13.5px] leading-relaxed text-muted">
            Assist scans your orders, supplier threads, and open alerts on a schedule. While the next pass runs, check your queue or create a recurring workflow.
          </p>
          <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
            <Button size="sm" variant="secondary" onClick={load}>
              <RefreshCw className="size-3.5" aria-hidden />
              <span>Refresh suggestions</span>
            </Button>
            <Link
              href="/assist"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border-strong bg-bg px-3 text-[13px] font-medium text-fg transition-colors hover:bg-hover"
            >
              <span>Open Assist queue</span>
            </Link>
            <Link
              href="/assist?show=workflows"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-opacity hover:opacity-90"
            >
              <span>Create workflow</span>
            </Link>
          </div>
        </div>
      ) : visible.length === 0 ? (
        <div className="rounded-xl border border-border bg-surface px-6 py-12 text-center" data-testid="assist-suggestions-empty">
          <Sparkles className="mx-auto mb-2.5 size-5 text-muted" aria-hidden />
          <p className="text-[15px] font-medium text-fg">
            {away.length > 0 ? "You have handled every current suggestion." : "No proactive suggestions right now."}
          </p>
          <p className="mx-auto mt-1.5 max-w-[46ch] text-[13.5px] text-muted">
            When orders slip, supplier confirmations go quiet, or records need a follow-up, they appear here as structured cards with drafts ready to review.
          </p>
          <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
            {away.length > 0 && (
              <Button size="sm" variant="secondary" onClick={() => setPutAway(true)}>
                Show {away.length} done, snoozed or dismissed
              </Button>
            )}
            <Link
              href="/assist?show=workflows"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-opacity hover:opacity-90"
            >
              Create a recurring workflow
            </Link>
          </div>
        </div>
      ) : (
        <section
          className="space-y-3"
          aria-label="Spotted opportunities and follow-ups"
          data-testid="assist-suggestions-list"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-[12.5px] font-semibold uppercase tracking-wide text-faint">
              Spotted for you ({visible.length})
            </h3>
            <div className="flex items-center gap-3 text-[12.5px]">
              {away.length > 0 && (
                <button
                  type="button"
                  onClick={() => setPutAway((v) => !v)}
                  className="text-muted hover:text-fg cursor-pointer"
                  data-testid="assist-suggestions-put-away"
                >
                  {putAway ? "Hide" : "Show"} {away.length} done, snoozed or dismissed
                </button>
              )}
              {data.more > 0 && (
                <Link href="/?show=alerts" className="text-accent hover:underline">
                  {plural(data.more, "more record")} in Alerts
                </Link>
              )}
            </div>
          </div>

          <div className="divide-y divide-border border-y border-border">
            {visible.map((s, idx) => (
              <SuggestionActionCard
                key={s.id}
                s={s}
                defaultDraftOpen={idx === 0}
                state={states[s.id]}
                onDone={onActionDone}
                workflowId={createdIds[s.id]}
                creating={creatingId === s.id}
                creatingAny={creatingId !== null}
                onMakeWorkflow={() => void turnSuggestionIntoWorkflow(s)}
              />
            ))}
          </div>

          <div className="space-y-1 pt-2 text-[12.5px] leading-relaxed text-faint">
            {extra && <p>{extra}</p>}
            <p>{SERVER_NOTE}</p>
          </div>
        </section>
      )}

      {routines.length > 0 && (
        <section
          className="space-y-3 border-t border-border pt-6"
          aria-label="Spotted recurring work"
          data-testid="assist-mined-routines"
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Clock3 className="size-3.5 text-muted" aria-hidden />
              <h3 className="text-[12.5px] font-semibold uppercase tracking-wide text-faint">
                Recurring work spotted from your habits ({routines.length})
              </h3>
            </div>
            <Link href="/assist?show=workflows" className="text-[12.5px] font-medium text-accent hover:underline">
              Manage all workflows
            </Link>
          </div>
          <ul className="divide-y divide-border border-y border-border">
            {routines.map((r) => {
              const wfId = createdIds[r.routine_id];
              const isBusy = creatingId === r.routine_id;
              return (
                <li
                  key={r.routine_id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3.5"
                >
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <p className="text-[14px] font-medium text-fg">{r.suggested_name || r.description}</p>
                    <p className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[12.5px] text-muted">
                      <span>{r.suggested_trigger_summary}</span>
                      {r.description && r.suggested_name && r.description !== r.suggested_name && (
                        <>
                          <span className="h-3 w-px bg-border" aria-hidden />
                          <span>{r.description}</span>
                        </>
                      )}
                    </p>
                  </div>
                  <div className="shrink-0">
                    {wfId ? (
                      <Link
                        href="/assist?show=workflows"
                        className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-ok hover:underline"
                      >
                        <CheckCircle2 className="size-3.5" aria-hidden />
                        Active in Workflows
                      </Link>
                    ) : (
                      <Button
                        size="sm"
                        variant="secondary"
                        className="h-7 rounded-md px-2.5 text-[12px]"
                        loading={isBusy}
                        disabled={creatingId !== null}
                        onClick={() => void turnRoutineIntoWorkflow(r)}
                        data-testid="routine-make-workflow"
                      >
                        <Repeat className="size-3.5" aria-hidden />
                        <span>Turn into workflow</span>
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
