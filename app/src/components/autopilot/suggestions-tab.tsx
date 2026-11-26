"use client";

import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  Copy,
  Mail,
  RefreshCw,
  Repeat,
  Search,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActionDialog } from "@/components/card-actions";
import { collapsedLine, draftText, etaBadge, suggestionHeadline } from "@/components/proactive-suggestions";
import { SourceChip } from "@/components/source-chip";
import { Button } from "@/components/ui/button";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { WritePanel } from "@/components/write-panel";
import { actionChip, isPutAway, splitAddresses, type Preset } from "@/lib/actions";
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
import type { ActionResult, CardActionOffer, ProactiveSuggestion, SuggestionDraft, Suggestions } from "@/lib/types";

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

function hasMailVerification(s: ProactiveSuggestion): boolean {
  if (s.eta?.in_mail) return true;
  return (s.reasoning ?? []).some((r) => /mail|email|teams/i.test(r.source?.system ?? ""));
}

function evidenceBadgeOf(s: ProactiveSuggestion): { label: string; status: Status } {
  if (s.confidence?.level === "high") {
    return { label: hasMailVerification(s) ? "Mail verified" : "Verified", status: "ok" };
  }
  if (s.confidence?.level === "medium") {
    return { label: hasMailVerification(s) ? "Mail verified" : "ERP due date", status: "risk" };
  }
  return { label: "From ERP schedule", status: "neutral" };
}

function resolveDraftRecipient(to: SuggestionDraft["to"] | string | string[] | null | undefined): {
  display: string | null;
  email: string;
  name: string | null;
  hasAddress: boolean;
} {
  if (!to) return { display: null, email: "", name: null, hasAddress: false };
  if (typeof to === "string") {
    const trimmed = to.trim();
    return { display: trimmed || null, email: trimmed.includes("@") ? trimmed : "", name: trimmed || null, hasAddress: trimmed.includes("@") };
  }
  if (Array.isArray(to)) {
    const list = to.map((x) => String(x ?? "").trim()).filter(Boolean);
    const emails = list.filter((x) => x.includes("@"));
    return {
      display: list.length ? list.join(", ") : null,
      email: emails.join(", "),
      name: list[0] ?? null,
      hasAddress: emails.length > 0,
    };
  }
  if (to.address?.trim()) {
    const addr = to.address.trim();
    return {
      display: to.name ? `${to.name} <${addr}>` : addr,
      email: addr,
      name: to.name?.trim() || addr,
      hasAddress: true,
    };
  }
  return { display: to.name?.trim() || null, email: "", name: to.name?.trim() || null, hasAddress: false };
}

function isReadyToSend(s: ProactiveSuggestion): boolean {
  if (!s.draft) return false;
  if (s.draft.audience === "internal") return true;
  const { hasAddress } = resolveDraftRecipient(s.draft.to as SuggestionDraft["to"] | string | string[] | null | undefined);
  return hasAddress;
}

function supplierNameOf(s: ProactiveSuggestion): string {
  const recipient = resolveDraftRecipient(s.draft?.to as SuggestionDraft["to"] | string | string[] | null | undefined);
  if (recipient.name) return recipient.name;
  if (s.subject?.label) return s.subject.label;
  return "Follow-up";
}

/** Right-pane Interactive Draft Studio for the selected suggestion. */
function SuggestionDraftStudio({
  s,
  state,
  onDone,
  onLocalDismiss,
  workflowId,
  creating,
  creatingAny,
  onMakeWorkflow,
  focusToSignal,
}: {
  s: ProactiveSuggestion;
  state?: ActionResult | null;
  onDone: (r: ActionResult) => void;
  onLocalDismiss: (id: string) => void;
  workflowId?: string;
  creating: boolean;
  creatingAny: boolean;
  onMakeWorkflow: () => void;
  focusToSignal: number;
}) {
  const recipient = resolveDraftRecipient(s.draft?.to as SuggestionDraft["to"] | string | string[] | null | undefined);
  const [draftTo, setDraftTo] = useState(recipient.email);
  const [draftSubject, setDraftSubject] = useState(s.draft?.subject ?? "");
  const [draftBody, setDraftBody] = useState(s.draft?.body ?? "");
  const [copied, setCopied] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [activeOffer, setActiveOffer] = useState<CardActionOffer | null>(null);
  const toInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusToSignal > 0) {
      toInputRef.current?.focus();
    }
  }, [focusToSignal]);

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
  const impactOrders = s.impact?.orders ?? [];
  const etaWords = s.eta ? readable(s.eta.text).trim() : "";
  const reasons = (s.reasoning ?? []).filter((r) => !etaWords || !readable(r.text).includes(etaWords));
  const hasEvidence = Boolean(s.eta || reasons.length > 0 || (s.missing ?? []).length > 0 || s.confidence?.why);

  const isInternal = s.draft?.audience === "internal";
  const effectiveEmails = splitAddresses(draftTo);
  const needsEmail = Boolean(s.draft && !isInternal && effectiveEmails.length === 0);
  const toPlaceholder =
    s.draft?.audience === "customer" ? "Add customer email to send" : "Add supplier email to send";
  const draftHeading =
    s.draft?.audience === "vendor"
      ? "Draft to the supplier"
      : s.draft?.audience === "customer"
        ? "Draft to the customer"
        : "Internal note";

  const offers = s.actions ?? [];
  const sendOffer = offers.find((o) => o.kind === "send_email" || o.kind === "reply_in_thread") ?? null;
  const dismissOffer = offers.find((o) => o.kind === "dismiss") ?? null;
  const snoozeOffer = offers.find((o) => o.kind === "snooze") ?? null;
  const extraOffers = offers.filter(
    (o) => o.kind !== "send_email" && o.kind !== "reply_in_thread" && o.kind !== "dismiss" && o.kind !== "snooze",
  );

  const preset: Preset | undefined = useMemo(() => {
    const addresses = splitAddresses(draftTo);
    return addresses.length > 0 ? { to: addresses } : undefined;
  }, [draftTo]);

  const copyLiveDraft = () => {
    const existingTo =
      s.draft?.to && typeof s.draft.to === "object" && !Array.isArray(s.draft.to) ? s.draft.to : null;
    const text = s.draft
      ? draftText({
          ...s.draft,
          to: draftTo.trim()
            ? {
                ...(existingTo ?? { basis: "" }),
                address: draftTo.trim(),
              }
            : s.draft.to,
          subject: draftSubject,
          body: draftBody,
        })
      : `${headline}\n\n${s.action?.text ?? ""}`;
    navigator.clipboard?.writeText(text).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      },
      () => undefined,
    );
  };

  return (
    <div className="space-y-6" data-testid="suggestion-detail-studio">
      {/* Context Header */}
      <header className="space-y-3 border-b border-border pb-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
              <Link
                href={href}
                className="text-[18px] font-semibold leading-[1.35] tracking-[-0.02em] text-fg hover:text-accent hover:underline"
              >
                {headline}
              </Link>
              {lateTag && <span className="num text-[12.5px] font-medium text-late">{lateTag}</span>}
              {amount && <span className="num text-[13px] font-semibold text-fg">{amount}</span>}
            </div>
            {s.action?.text && (
              <p className="max-w-[68ch] text-[14px] leading-[1.6] text-muted">{s.action.text}</p>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {chip && (
              <span
                data-testid="action-state"
                title={chip.title}
                className={cn("inline-flex items-center gap-1.5 text-[12px] font-medium", statusTextClass(chip.status))}
              >
                <span className={cn("size-1.5 shrink-0 rounded-full", statusDotClass(chip.status))} aria-hidden />
                <span>{chip.label}</span>
              </span>
            )}
            <Link
              href={href}
              className="inline-flex h-8 items-center gap-1 rounded-md border border-border bg-surface px-2.5 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2"
            >
              <span>Open record</span>
              <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
            </Link>
          </div>
        </div>

        {/* Context Metadata Strip: ERP due date / Mail verification / Order linkage */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[12.5px] text-muted">
          <span
            data-testid="suggestion-evidence-pill"
            className={cn(
              "inline-flex items-center gap-1.5 font-medium",
              evidence.status === "ok" ? "text-ok" : evidence.status === "risk" ? "text-risk" : "text-muted",
            )}
          >
            <span className={cn("size-1.5 shrink-0 rounded-full", statusDotClass(evidence.status))} aria-hidden />
            <span>{evidence.label}</span>
          </span>

          {eta && (
            <>
              <span className="text-border-strong" aria-hidden>
                ·
              </span>
              <span
                data-testid="suggestion-eta"
                className={cn(
                  "font-medium",
                  eta.status === "late" ? "text-late" : eta.status === "ok" ? "text-ok" : eta.status === "risk" ? "text-risk" : "text-muted",
                )}
              >
                {eta.label}
              </span>
            </>
          )}

          {impactOrders.length > 0 && (
            <>
              <span className="text-border-strong" aria-hidden>
                ·
              </span>
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
            </>
          )}
        </div>
      </header>

      {/* Interactive Draft Studio Surface (#14161b elevated email card) */}
      <section
        className="overflow-hidden rounded-xl border border-border/80 bg-surface dark:bg-[#14161b]"
        aria-label="Draft studio"
        data-testid="suggestion-draft"
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/80 px-4 py-2.5">
          <div className="inline-flex items-center gap-2 text-[12.5px] font-medium text-fg">
            <Mail className="size-3.5 text-muted" aria-hidden />
            <span>{s.draft ? draftHeading : "Follow-up summary"}</span>
            <span className="font-normal text-faint">(not sent)</span>
          </div>
          {needsEmail && (
            <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-risk">
              <span className="size-1.5 rounded-full bg-dot-risk" aria-hidden />
              <span>Needs email</span>
            </span>
          )}
        </div>

        {s.draft ? (
          <>
            <div className="divide-y divide-border/70 border-b border-border/80 text-[13px]">
              <div className="flex items-center gap-3 px-4 py-2" data-testid="draft-to">
                <label htmlFor={`studio-to-${s.id}`} className="w-14 shrink-0 text-[12px] font-medium text-faint">
                  To
                </label>
                {isInternal ? (
                  <span className="min-w-0 flex-1 text-fg">{recipient.display ?? "Internal team"}</span>
                ) : (
                  <div className="flex min-w-0 flex-1 items-center gap-2">
                    {recipient.name && !recipient.hasAddress && (
                      <span className="shrink-0 text-[12.5px] text-muted">{recipient.name}</span>
                    )}
                    <input
                      ref={toInputRef}
                      id={`studio-to-${s.id}`}
                      type="email"
                      value={draftTo}
                      onChange={(e) => setDraftTo(e.target.value)}
                      placeholder={toPlaceholder}
                      aria-label={toPlaceholder}
                      data-testid="draft-to-input"
                      className="h-7 min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-2 text-[13px] text-fg transition-colors hover:border-border focus:border-border-strong focus:bg-bg focus:outline-none placeholder:text-faint"
                    />
                  </div>
                )}
              </div>

              <div className="flex items-center gap-3 px-4 py-2">
                <label htmlFor={`studio-subject-${s.id}`} className="w-14 shrink-0 text-[12px] font-medium text-faint">
                  Subject
                </label>
                <input
                  id={`studio-subject-${s.id}`}
                  type="text"
                  value={draftSubject}
                  onChange={(e) => setDraftSubject(e.target.value)}
                  data-testid="draft-subject-input"
                  className="h-7 min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-2 text-[13px] font-medium text-fg transition-colors hover:border-border focus:border-border-strong focus:bg-bg focus:outline-none"
                />
              </div>
            </div>

            <div className="px-4 py-3.5">
              <label htmlFor={`studio-body-${s.id}`} className="sr-only">
                Draft message body
              </label>
              <textarea
                id={`studio-body-${s.id}`}
                value={draftBody}
                onChange={(e) => setDraftBody(e.target.value)}
                rows={8}
                data-testid="draft-body-input"
                className="w-full resize-y bg-transparent font-sans text-[13.5px] leading-[1.65] text-fg focus:outline-none"
              />
            </div>

            {(s.draft.facts ?? []).length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 border-t border-border/70 px-4 py-2 text-[11.5px]">
                <span className="text-faint">Figures from</span>
                {(s.draft.facts ?? []).slice(0, 5).map((f, n) =>
                  f?.source ? (
                    <SourceChip key={`${f.source.handle ?? "src"}-${n}`} source={f.source} record={readable(f.value ?? "")} />
                  ) : null,
                )}
              </div>
            )}
          </>
        ) : (
          <div className="px-4 py-5 text-[13.5px] leading-[1.6] text-muted">
            No outgoing draft is prepared for this item yet. Open the record to inspect details or turn it into a recurring workflow.
          </div>
        )}

        {/* Unmistakable Bottom Action Bar: Send email, Copy draft, Turn into workflow, Dismiss */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/80 bg-bg-subtle/60 dark:bg-[#111318] px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            {s.draft && (
              <button
                type="button"
                disabled={needsEmail && !sendOffer}
                onClick={() => {
                  if (sendOffer) {
                    setActiveOffer(sendOffer);
                  } else if (needsEmail) {
                    toInputRef.current?.focus();
                  } else {
                    copyLiveDraft();
                  }
                }}
                data-testid="suggestion-send-email"
                className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3.5 text-[12.5px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-40 cursor-pointer"
              >
                <Send className="size-3.5" aria-hidden />
                <span>Send email</span>
              </button>
            )}

            <button
              type="button"
              onClick={copyLiveDraft}
              data-testid="copy-draft"
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2 cursor-pointer"
            >
              {copied ? <Check className="size-3.5 text-ok" aria-hidden /> : <Copy className="size-3.5 text-muted" aria-hidden />}
              <span>{copied ? "Copied" : "Copy draft"}</span>
            </button>

            {workflowId ? (
              <span
                className="inline-flex items-center gap-1.5 px-1 text-[12.5px] font-medium text-ok"
                data-testid="suggestion-workflow-created"
              >
                <CheckCircle2 className="size-3.5" aria-hidden />
                <span>Added as workflow</span>
                <Link href="/assist?show=workflows" className="text-accent underline underline-offset-2">
                  Open
                </Link>
              </span>
            ) : (
              <button
                type="button"
                disabled={creating || creatingAny}
                onClick={onMakeWorkflow}
                data-testid="suggestion-make-workflow"
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2 disabled:opacity-50 cursor-pointer"
              >
                <Repeat className="size-3.5 text-muted" aria-hidden />
                <span>{creating ? "Saving..." : "Turn into workflow"}</span>
              </button>
            )}

            {snoozeOffer && (
              <button
                type="button"
                onClick={() => setActiveOffer(snoozeOffer)}
                className="inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
              >
                <Clock3 className="size-3.5" aria-hidden />
                <span>{snoozeOffer.label}</span>
              </button>
            )}

            {extraOffers.map((o) => (
              <button
                key={o.kind}
                type="button"
                onClick={() => setActiveOffer(o)}
                className="inline-flex h-8 items-center rounded-md px-2 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
              >
                {o.label}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => {
              if (dismissOffer) setActiveOffer(dismissOffer);
              else onLocalDismiss(s.id);
            }}
            data-testid="suggestion-dismiss"
            className="inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-late cursor-pointer"
          >
            <Trash2 className="size-3.5" aria-hidden />
            <span>Dismiss</span>
          </button>
        </div>
      </section>

      {/* Collapsible Evidence & Checks */}
      {hasEvidence && (
        <section className="space-y-2.5">
          <button
            type="button"
            aria-expanded={evidenceOpen}
            onClick={() => setEvidenceOpen((v) => !v)}
            data-testid="suggestion-toggle"
            className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
          >
            {evidenceOpen ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
            <span>Evidence and checks</span>
          </button>

          {evidenceOpen && (
            <div
              className="space-y-3 rounded-xl border border-border bg-surface/50 p-4 text-[13px]"
              data-testid="suggestion-detail"
            >
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
                <ul className="space-y-1.5 text-fg/90" data-testid="suggestion-reasoning">
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
                  <p className="mb-1 text-[12px] font-medium text-faint">Not on record</p>
                  <ul className="list-disc space-y-0.5 pl-4 text-muted">
                    {(s.missing ?? []).map((m, idx) => (
                      <li key={idx}>{m}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </section>
      )}

      <p className="text-[12px] leading-relaxed text-faint">{SERVER_NOTE}</p>

      {activeOffer && (
        <ActionDialog
          card={s}
          offer={activeOffer}
          state={state}
          preset={activeOffer.kind === "send_email" ? preset : undefined}
          onClose={() => setActiveOffer(null)}
          onDone={(r) => {
            setActiveOffer(null);
            onDone(r);
          }}
        />
      )}
    </div>
  );
}

/**
 * Ground-Up 38% / 62% Split-Pane Suggestions Triage:
 * - Compact top control strip: follow-up count, Liquid Glass Ready to send | All switcher, search.
 * - Left recessed triage rail (#121418): PO/record ref, supplier, urgency, quiet Ready to send / Needs email state.
 * - Right Draft Studio (#0d0e11 + elevated #14161b email card): full context, live editable To:/Subject:/Body,
 *   and unmistakable Send email, Copy draft, Turn into workflow, and Dismiss controls.
 */
export function AssistSuggestionsView() {
  const [data, setData] = useState<Suggestions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const [localDismissed, setLocalDismissed] = useState<Set<string>>(() => new Set());
  const [putAway, setPutAway] = useState(false);
  const [viewFilter, setViewFilter] = useState<"ready" | "all" | null>(null);
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  const [focusToSignal, setFocusToSignal] = useState(0);
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

  const onLocalDismiss = useCallback((id: string) => {
    setLocalDismissed((prev) => new Set(prev).add(id));
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
  const away = useMemo(
    () => every.filter((s) => isPutAway(states[s.id]) || localDismissed.has(s.id)),
    [every, states, localDismissed],
  );
  const visible = useMemo(
    () => (putAway ? every : every.filter((s) => !isPutAway(states[s.id]) && !localDismissed.has(s.id))),
    [every, putAway, states, localDismissed],
  );
  const readyItems = useMemo(() => visible.filter(isReadyToSend), [visible]);
  const effectiveFilter: "ready" | "all" = viewFilter ?? (readyItems.length > 0 ? "ready" : "all");

  const displayed = useMemo(() => {
    const base = effectiveFilter === "ready" ? readyItems : visible;
    const q = search.trim().toLowerCase();
    if (!q) return base;
    return base.filter((s) => {
      const head = suggestionHeadline(s);
      const supplier = supplierNameOf(s);
      const text = `${head} ${supplier} ${s.action?.text ?? ""} ${s.draft?.subject ?? ""}`.toLowerCase();
      return text.includes(q);
    });
  }, [effectiveFilter, readyItems, visible, search]);

  useEffect(() => {
    if (displayed.length === 0) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !displayed.some((s) => s.id === selectedId)) {
      setSelectedId(displayed[0].id);
    }
  }, [displayed, selectedId]);

  const activeSuggestion = useMemo(
    () => displayed.find((s) => s.id === selectedId) ?? displayed[0] ?? null,
    [displayed, selectedId],
  );

  const extra = collapsedLine(data?.collapsed);
  const suggestionsReady = Boolean(
    data && (data.status === "read" || data.status === ("ok" as string) || every.length > 0),
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-bg" data-testid="assist-suggestions-view">
      {/* Compact Top Control Bar */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2.5 border-b border-border bg-bg px-4 py-2 lg:px-5">
        <div className="flex flex-wrap items-center gap-3">
          <span className="num text-[13px] font-medium text-fg">
            {readyItems.length > 0
              ? `${readyItems.length} ${readyItems.length === 1 ? "follow-up" : "follow-ups"} ready`
              : `${visible.length} ${visible.length === 1 ? "follow-up" : "follow-ups"} open`}
          </span>

          <div
            role="group"
            aria-label="Filter suggestions"
            data-testid="suggestions-filter-toggle"
            className="liquid-glass-pill inline-flex items-center gap-0.5 rounded-lg p-0.5"
          >
            <button
              type="button"
              aria-pressed={effectiveFilter === "ready"}
              onClick={() => setViewFilter("ready")}
              data-testid="suggestions-filter-ready"
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                effectiveFilter === "ready" ? "liquid-glass-active" : "text-muted hover:text-fg",
              )}
            >
              <span>Ready to send</span>
              <span className="num text-[11px] opacity-75">{readyItems.length}</span>
            </button>
            <button
              type="button"
              aria-pressed={effectiveFilter === "all"}
              onClick={() => setViewFilter("all")}
              data-testid="suggestions-filter-all"
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                effectiveFilter === "all" ? "liquid-glass-active" : "text-muted hover:text-fg",
              )}
            >
              <span>All</span>
              <span className="num text-[11px] opacity-75">{visible.length}</span>
            </button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-56 max-w-full">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search suggestions..."
              aria-label="Search suggestions"
              className="h-8 w-full rounded-md border border-border bg-surface pl-8 pr-7 text-[12.5px] text-fg placeholder:text-faint focus:border-border-strong focus:outline-none"
            />
            {search && (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => setSearch("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer text-muted hover:text-fg"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            )}
          </div>

          {away.length > 0 && (
            <button
              type="button"
              onClick={() => setPutAway((v) => !v)}
              className="h-8 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
              data-testid="assist-suggestions-put-away"
            >
              {putAway ? "Hide" : "Show"} {away.length} handled
            </button>
          )}
        </div>
      </div>

      <StaleNote since={data?.stale_since} className="shrink-0 px-4 py-1.5 lg:px-5" />

      {workflowError && (
        <div role="alert" className="shrink-0 border-b border-border bg-surface px-4 py-2 text-[13px] text-late lg:px-5">
          {workflowError}
        </div>
      )}

      {error ? (
        <div className="p-6">
          <LoadProblem message={error} onRetry={load} />
        </div>
      ) : !data ? (
        <div className="p-6">
          <Pending label="Reading proactive suggestions" />
        </div>
      ) : !suggestionsReady ? (
        <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center" data-testid="assist-suggestions-not-ready">
          <Sparkles className="mx-auto mb-3 size-5 text-muted" aria-hidden />
          <h2 className="text-[15px] font-semibold text-fg">Suggestions are not ready yet</h2>
          <p className="mx-auto mt-1.5 max-w-[46ch] text-[13.5px] leading-relaxed text-muted">
            Assist scans orders, supplier threads, and open alerts on a schedule. Refresh or create a recurring workflow.
          </p>
          <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
            <Button size="sm" variant="secondary" onClick={load}>
              <RefreshCw className="size-3.5" aria-hidden />
              <span>Refresh</span>
            </Button>
            <Link
              href="/assist?show=workflows"
              className="inline-flex h-8 items-center rounded-md bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-opacity hover:opacity-90"
            >
              Create workflow
            </Link>
          </div>
        </div>
      ) : visible.length === 0 && routines.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center" data-testid="assist-suggestions-empty">
          <Sparkles className="mx-auto mb-2.5 size-5 text-muted" aria-hidden />
          <p className="text-[15px] font-medium text-fg">
            {away.length > 0 ? "You have handled every current suggestion." : "No proactive suggestions right now."}
          </p>
          <p className="mx-auto mt-1.5 max-w-[46ch] text-[13.5px] text-muted">
            When orders slip or supplier threads go quiet, follow-up drafts appear here ready for review.
          </p>
          <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
            {away.length > 0 && (
              <Button size="sm" variant="secondary" onClick={() => setPutAway(true)}>
                Show {away.length} handled
              </Button>
            )}
            <Link
              href="/assist?show=workflows"
              className="inline-flex h-8 items-center rounded-md bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-opacity hover:opacity-90"
            >
              Create a workflow
            </Link>
          </div>
        </div>
      ) : (
        /* 38% / 62% Split Triage matching Inbox craft */
        <div
          className="grid h-[calc(100dvh-148px)] min-h-[560px] flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[minmax(340px,38%)_1fr]"
          data-testid="assist-suggestions-list"
        >
          {/* Left Triage Rail */}
          <aside
            aria-label="Suggestions list"
            className={cn(
              "min-h-0 flex-col overflow-y-auto border-b border-border bg-list dark:bg-[#121418] lg:flex lg:border-r lg:border-b-0",
              mobileDetailOpen && activeSuggestion ? "hidden lg:flex" : "flex",
            )}
          >
            {displayed.length === 0 ? (
              <div className="px-4 py-12 text-center text-[13px] text-muted" data-testid="suggestions-ready-empty">
                <p>No drafts match this filter right now.</p>
                {effectiveFilter === "ready" && visible.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setViewFilter("all")}
                    className="mt-2 font-medium text-accent hover:underline cursor-pointer"
                  >
                    Show all {visible.length} suggestions
                  </button>
                )}
              </div>
            ) : (
              <ul className="divide-y divide-border/70">
                {displayed.map((s) => {
                  const isSelected = activeSuggestion?.id === s.id;
                  const headline = suggestionHeadline(s);
                  const supplier = supplierNameOf(s);
                  const lateTag = extractDaysLate(headline);
                  const ready = isReadyToSend(s);
                  const evidence = evidenceBadgeOf(s);
                  const recipient = resolveDraftRecipient(s.draft?.to as SuggestionDraft["to"] | string | string[] | null | undefined);
                  const missingEmail = Boolean(s.draft && s.draft.audience !== "internal" && !recipient.hasAddress);
                  const missingEmailText = s.draft?.audience === "customer" ? "Needs customer email" : "Needs supplier email";

                  return (
                    <li key={s.id} data-testid="suggestion">
                      <div
                        className={cn(
                          "transition-colors",
                          isSelected ? "bg-row-on dark:bg-[#1b1e25]" : "hover:bg-hover",
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => {
                            setSelectedId(s.id);
                            setMobileDetailOpen(true);
                          }}
                          aria-current={isSelected ? "true" : undefined}
                          className="w-full px-3.5 py-3 text-left cursor-pointer"
                        >
                          <div className="flex items-center justify-between gap-2 text-[12px]">
                            <span className="min-w-0 truncate font-medium text-fg/80">{supplier}</span>
                            {lateTag ? (
                              <span className="num shrink-0 font-medium text-late">{lateTag}</span>
                            ) : (
                              <span className="shrink-0 text-[11.5px] text-faint">{evidence.label}</span>
                            )}
                          </div>

                          <p
                            data-testid="suggestion-headline"
                            className="mt-1 truncate text-[13.5px] font-semibold leading-snug text-fg"
                          >
                            {headline}
                          </p>

                          <div className="mt-1.5 flex items-center justify-between gap-2 text-[12px]">
                            <span className="min-w-0 flex-1 truncate text-muted">
                              {s.action?.text || evidence.label}
                            </span>
                            <span
                              className={cn(
                                "inline-flex shrink-0 items-center gap-1.5 text-[11.5px] font-medium",
                                ready ? "text-ok" : "text-risk",
                              )}
                            >
                              <span
                                className={cn("size-1.5 shrink-0 rounded-full", ready ? "bg-dot-ok" : "bg-dot-risk")}
                                aria-hidden
                              />
                              <span>{ready ? "Ready to send" : "Needs email"}</span>
                            </span>
                          </div>
                        </button>

                        {missingEmail && (
                          <div className="flex items-center justify-between gap-2 px-3.5 pb-2.5 text-[11.5px]">
                            <span className="text-faint">{evidence.label}</span>
                            <button
                              type="button"
                              onClick={() => {
                                setSelectedId(s.id);
                                setMobileDetailOpen(true);
                                setFocusToSignal((n) => n + 1);
                              }}
                              data-testid="suggestion-needs-email"
                              className="font-medium text-accent hover:underline cursor-pointer"
                            >
                              {missingEmailText}
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}

            {/* Quiet footer summary + spotted recurring patterns */}
            {(extra || data.more > 0) && (
              <div className="border-t border-border px-3.5 py-3 text-[12px] leading-relaxed text-faint">
                {extra && <p>{extra}</p>}
                {data.more > 0 && (
                  <Link href="/?show=alerts" className="mt-1 inline-block font-medium text-accent hover:underline">
                    {plural(data.more, "more record")} in Alerts
                  </Link>
                )}
              </div>
            )}

            {routines.length > 0 && (
              <section
                className="border-t border-border px-3.5 py-3.5"
                aria-label="Spotted recurring work"
                data-testid="assist-mined-routines"
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted">
                    <Clock3 className="size-3.5" aria-hidden />
                    <span>Recurring habits ({routines.length})</span>
                  </span>
                  <Link href="/assist?show=workflows" className="text-[11.5px] font-medium text-accent hover:underline">
                    Workflows
                  </Link>
                </div>
                <ul className="space-y-2">
                  {routines.map((r) => {
                    const wfId = createdIds[r.routine_id];
                    const isBusy = creatingId === r.routine_id;
                    return (
                      <li
                        key={r.routine_id}
                        className="rounded-lg border border-border bg-bg dark:bg-[#0d0e11] p-2.5 text-[12.5px]"
                      >
                        <p className="font-medium text-fg">{r.suggested_name || r.description}</p>
                        <p className="mt-0.5 text-[11.5px] text-muted">{r.suggested_trigger_summary}</p>
                        <div className="mt-2">
                          {wfId ? (
                            <Link
                              href="/assist?show=workflows"
                              className="inline-flex items-center gap-1 text-[12px] font-medium text-ok hover:underline"
                            >
                              <CheckCircle2 className="size-3.5" aria-hidden />
                              <span>Active in Workflows</span>
                            </Link>
                          ) : (
                            <button
                              type="button"
                              disabled={isBusy || creatingId !== null}
                              onClick={() => void turnRoutineIntoWorkflow(r)}
                              data-testid="routine-make-workflow"
                              className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 text-[11.5px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2 disabled:opacity-50 cursor-pointer"
                            >
                              <Repeat className="size-3" aria-hidden />
                              <span>{isBusy ? "Saving..." : "Turn into workflow"}</span>
                            </button>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            )}
          </aside>

          {/* Right Interactive Draft Studio (#0d0e11 in dark) */}
          <section
            aria-label="Selected suggestion draft studio"
            className={cn(
              "min-h-0 min-w-0 overflow-y-auto bg-bg dark:bg-[#0d0e11] px-5 py-5 lg:block lg:px-7 lg:py-6",
              mobileDetailOpen && activeSuggestion ? "block" : "hidden lg:block",
            )}
          >
            <div className="mb-4 border-b border-border pb-3 lg:hidden">
              <button
                type="button"
                onClick={() => setMobileDetailOpen(false)}
                className="inline-flex items-center gap-1.5 text-[13px] font-medium text-muted transition-colors hover:text-fg cursor-pointer"
              >
                <ArrowLeft className="size-3.5" aria-hidden />
                <span>Back to suggestions</span>
              </button>
            </div>

            <div className="max-w-3xl">
              {activeSuggestion ? (
                <SuggestionDraftStudio
                  key={activeSuggestion.id}
                  s={activeSuggestion}
                  state={states[activeSuggestion.id]}
                  onDone={onActionDone}
                  onLocalDismiss={onLocalDismiss}
                  workflowId={createdIds[activeSuggestion.id]}
                  creating={creatingId === activeSuggestion.id}
                  creatingAny={creatingId !== null}
                  onMakeWorkflow={() => void turnSuggestionIntoWorkflow(activeSuggestion)}
                  focusToSignal={focusToSignal}
                />
              ) : (
                <div className="py-16 text-center text-[13.5px] text-muted">
                  Select a suggestion from the list to review and edit its draft.
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
