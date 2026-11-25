"use client";

import { Mail } from "lucide-react";
import { useState } from "react";
import { ActionDialog } from "@/components/card-actions";
import { useViewingAs } from "@/components/view-as";
import { prepareEmailDraft } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { ActionResult, CardActionOffer, EmailDraftPrepared, EmailDraftRequest } from "@/lib/types";

const SEND_OFFER: CardActionOffer = { kind: "send_email", label: "Send email", outbound: true };

/**
 * Draft an email for a brief, answer or chart (email only, never Teams) and open the
 * confirm-before-send ActionDialog so the person reviews recipients, body and attachment
 * before anything goes out.
 */
export function EmailDraftButton({
  title,
  summary = "",
  charts,
  citations,
  format = "xlsx",
  attachFile = false,
  label = "Email this",
  className = "inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer disabled:cursor-default disabled:opacity-50",
}: {
  title: string;
  summary?: string;
  charts?: Record<string, unknown>[];
  citations?: Record<string, unknown>[];
  format?: EmailDraftRequest["format"];
  attachFile?: boolean;
  label?: string;
  className?: string;
}) {
  const viewing = useViewingAs();
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<EmailDraftPrepared | null>(null);
  const [sentNote, setSentNote] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  if (viewing) return null;

  const open = async () => {
    setBusy(true);
    setProblem(null);
    setSentNote(null);
    try {
      const got = await prepareEmailDraft({
        title: title.trim() || "Workbench brief",
        summary,
        charts: charts && charts.length > 0 ? charts : undefined,
        citations: citations && citations.length > 0 ? citations : undefined,
        format,
        attach_file: attachFile,
      });
      setDraft(got);
    } catch (err) {
      setProblem(plainError(err));
    } finally {
      setBusy(false);
    }
  };

  const done = (r: ActionResult) => {
    setDraft(null);
    setSentNote(r.status === "sent" ? "Email sent." : r.status === "dry_run" ? "Draft checked (dry run, not sent)." : `Email status: ${r.status}.`);
  };

  return (
    <span className="inline-flex items-center gap-x-2">
      <button type="button" className={className} disabled={busy} onClick={open} data-testid="email-draft-btn">
        <Mail className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
        {busy ? "Drafting" : label}
      </button>
      {sentNote && <span className="text-[12px] text-muted" data-testid="email-draft-done">{sentNote}</span>}
      {problem && <span className="text-[12px] text-late" role="alert">{problem}</span>}
      {draft && (
        <ActionDialog
          card={{ id: draft.card_id, subject: { label: draft.subject } }}
          offer={SEND_OFFER}
          onClose={() => setDraft(null)}
          onDone={done}
        />
      )}
    </span>
  );
}
