"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/notice";
import { assistDraft } from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
import { sourceHref } from "@/lib/format";
import type { AssistDraft } from "@/lib/types";

// Assist: the work a person starts themselves. One typed line ("tell Sam the PO 1234 is
// late") comes back as a drafted mail or Teams message: recipients, subject and body, each
// fact with its sources and what could not be settled. Nothing is sent: the person edits
// it, copies it or opens it in their own mail.

const field = "w-full min-w-0 rounded-lg border border-border bg-bg px-3 py-2 text-[14px] text-fg";

/** A mailto: link for the drafted mail (opens the person's own mail client; never sends). */
export function mailtoOf(d: AssistDraft, body: string): string {
  const to = (d.to ?? []).map((t) => t.address).join(",");
  const q = [d.subject ? `subject=${encodeURIComponent(d.subject)}` : "", `body=${encodeURIComponent(body)}`].filter(Boolean).join("&");
  return `mailto:${to}?${q}`;
}

export function AssistBox({ compact = false }: { compact?: boolean } = {}) {
  const [line, setLine] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<AssistDraft | null>(null);
  const [body, setBody] = useState("");
  const [copied, setCopied] = useState(false);
  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!line.trim() || busy) return;
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const d = await assistDraft(line.trim());
      setDraft(d);
      setBody(d.body ?? "");
    } catch (err) {
      setDraft(null);
      // the server's refusal is one of its fixed sentences (operator_api.assist_api.REFUSED)
      setError(err instanceof ApiError && err.code === "assist_refused" && err.detail ? err.detail : plainError(err));
    } finally {
      setBusy(false);
    }
  };
  const copy = async () => {
    await navigator.clipboard?.writeText([draft?.subject ? `Subject: ${draft.subject}` : "", body].filter(Boolean).join("\n\n"));
    setCopied(true);
  };
  return (
    <section className={compact ? "space-y-3" : "space-y-3 rounded-lg border border-border p-3"} aria-label="Assist" data-testid="assist">
      <form className={compact ? "flex flex-col gap-2" : "flex flex-col gap-2 sm:flex-row"} onSubmit={(e) => void run(e)}>
        <label className="sr-only" htmlFor="assist-line">
          What should be sent, and to whom
        </label>
        <input
          id="assist-line"
          className={field}
          value={line}
          maxLength={300}
          enterKeyHint="send"
          autoComplete="off"
          placeholder="Tell Sam the lot for order 1234 is on hold"
          onChange={(e) => setLine(e.target.value)}
        />
        <Button type="submit" size={compact ? "sm" : "md"} variant={compact ? "secondary" : "primary"} disabled={busy || !line.trim()} className={compact ? "self-start" : "w-full sm:w-auto"}>
          {busy ? "Drafting…" : "Draft"}
        </Button>
      </form>
      {error && <ErrorNotice message={error} />}
      {draft && (
        <div className="space-y-2 text-[13px]" data-testid="assist-draft">
          <p className="text-muted">{draft.channel === "teams" ? "Teams message" : "Mail"} draft. Nothing is sent: edit it, copy it or open it in your own mail.</p>
          <p className="break-words">
            <span className="text-muted">To: </span>
            {(draft.to ?? []).length ? (draft.to ?? []).map((t) => t.address).join(", ") : <span className="text-muted">nobody yet</span>}
          </p>
          {(draft.choices ?? []).map((c) => (
            <p key={c.mention} className="break-words">
              <span className="text-muted">“{c.mention}” could be: </span>
              {c.options.map((o) => o.address).join(", ")}
            </p>
          ))}
          {draft.withheld > 0 && <p className="text-muted">Someone you named can't be drafted to and was left out.</p>}
          {draft.subject && (
            <p className="break-words">
              <span className="text-muted">Subject: </span>
              {draft.subject}
            </p>
          )}
          <label className="sr-only" htmlFor="assist-body">
            Message
          </label>
          <textarea id="assist-body" className={`${field} min-h-40`} value={body} onChange={(e) => setBody(e.target.value)} />
          {(draft.facts ?? []).length > 0 && (
            <ul className="space-y-1" aria-label="Facts and their sources">
              {(draft.facts ?? []).map((f) => (
                <li key={f.text} className="break-words">
                  {f.text}{" "}
                  {f.sources.map((s) => (
                    <a key={s.handle} className="text-muted underline" href={s.open_url ?? sourceHref(s.handle)}>
                      {s.label}
                    </a>
                  ))}
                </li>
              ))}
            </ul>
          )}
          {(draft.gaps ?? []).length > 0 && (
            <ul className="list-disc pl-5 text-muted" aria-label="Not settled">
              {(draft.gaps ?? []).map((g) => (
                <li key={g}>{g}</li>
              ))}
            </ul>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => void copy()} disabled={!body}>
              {copied ? "Copied" : "Copy"}
            </Button>
            {draft.channel === "mail" && (draft.to ?? []).length > 0 && body && (
              <Button size="sm" variant="outline" asChild>
                <a href={mailtoOf(draft, body)}>Open in my mail</a>
              </Button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
