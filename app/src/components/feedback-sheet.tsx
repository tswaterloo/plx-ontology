"use client";

import { Check, MessageSquareText } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useCallback, useId, useState } from "react";
import { sendNoteFeedback } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import type { NoteFeedbackRequest } from "@/lib/types";
import { recordOfPage } from "./feedback-dialog";
import { Sheet } from "./ui/sheet";

type NoteKind = NonNullable<NoteFeedbackRequest["kind"]>;

/** The lists the app has: feedback on one is about the list. */
function isList(path: string): boolean {
  return path === "/" || path === "/late" || path === "/watching" || path === "/activity" || path === "/feedback";
}


const KINDS: { kind: NoteKind; label: string }[] = [
  { kind: "record", label: "This record" },
  { kind: "list", label: "This list" },
  { kind: "page", label: "This page" },
  { kind: "ui", label: "The app itself" },
  { kind: "general", label: "General" },
];


export default function FeedbackSheet({ onClose }: { onClose: () => void }) {
  const path = usePathname() || "/";
  const params = useSearchParams();
  const record = recordOfPage(path, params);
  const [kind, setKind] = useState<NoteKind>(record ? "record" : isList(path) ? "list" : "page");
  const [note, setNote] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);
  const noteId = useId();
  const page = (path + (params?.toString() ? `?${params.toString()}` : "")).slice(0, 300);
  const kinds = KINDS.filter((k) => k.kind !== "record" || record);

  const submit = useCallback(async () => {
    if (!note.trim() || state === "sending") return;
    setError(null);
    setState("sending");
    try {
      const body: NoteFeedbackRequest = { kind, note: note.trim().slice(0, 2000), page };
      if (record && kind === "record") Object.assign(body, { object_type: record.object_type, object_id: record.object_id, label: record.label });
      await sendNoteFeedback(body);
      setState("sent");
    } catch (e) {
      setError(plainError(e));
      setState("idle");
    }
  }, [kind, note, page, record, state]);

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title="Feedback"
      icon={<MessageSquareText className="size-4 text-muted" strokeWidth={2} aria-hidden />}
      description="Tell the Workbench team about this page, a record, a list or the app."
      footer={
        state === "sent" ? (
          <div className="flex justify-end">
            <button type="button" onClick={onClose} className="inline-flex h-8 items-center rounded-lg px-3 text-[13px] font-medium hover:bg-hover cursor-pointer">
              Close
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-end gap-1">
            <button type="button" onClick={onClose} className="inline-flex h-8 items-center rounded-lg px-3 text-[13px] text-muted hover:bg-hover hover:text-fg cursor-pointer">
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!note.trim() || state === "sending"}
              className="inline-flex h-8 items-center rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg hover:brightness-110 disabled:opacity-50 cursor-pointer disabled:cursor-default"
              data-testid="feedback-submit"
            >
              Submit feedback
            </button>
          </div>
        )
      }
    >
      {state === "sent" ? (
        <div role="status" className="space-y-2 pt-6 text-center" data-testid="feedback-sent">
          <Check className="mx-auto size-5 text-accent" strokeWidth={2.2} aria-hidden />
          <p className="text-[13px] font-medium">Sent to the owner.</p>
          <p className="text-[12px] text-muted">
            <Link href="/feedback" onClick={onClose} className="font-medium text-fg hover:underline">
              See your feedback
            </Link>
          </p>
        </div>
      ) : (
        <div className="space-y-5">
          <fieldset>
            <legend className="mb-2 text-[12px] text-muted">About</legend>
            <div className="flex flex-wrap gap-1.5" role="radiogroup">
              {kinds.map((k) => (
                <button
                  key={k.kind}
                  type="button"
                  role="radio"
                  aria-checked={kind === k.kind}
                  onClick={() => setKind(k.kind)}
                  className={cn(
                    "inline-flex h-7 items-center rounded-md px-2.5 text-[12px] cursor-pointer",
                    kind === k.kind ? "bg-accent-soft font-medium text-fg" : "bg-surface-2 text-fg hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]",
                  )}
                >
                  {k.label}
                </button>
              ))}
            </div>
          </fieldset>
          <div>
            <label htmlFor={noteId} className="mb-2 block text-[12px] text-muted">
              What should we know?
            </label>
            <textarea
              id={noteId}
              autoFocus
              value={note}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void submit();
                }
              }}
              rows={6}
              maxLength={2000}
              placeholder="What's wrong, missing or confusing, or what would help"
              className="w-full resize-none rounded-lg bg-surface px-3 py-2 text-[13px] leading-5 placeholder:text-faint focus:outline-2 focus:outline-ring"
            />
          </div>
          <dl className="space-y-1 text-[12px] leading-4 text-muted" data-testid="feedback-attached">
            <div className="flex gap-2">
              <dt className="w-16 shrink-0 text-faint">Page</dt>
              <dd className="min-w-0 truncate">{page}</dd>
            </div>
            {record && kind === "record" && (
              <div className="flex gap-2">
                <dt className="w-16 shrink-0 text-faint">Record</dt>
                <dd className="min-w-0 truncate">{record.label}</dd>
              </div>
            )}
            <p className="pt-1 text-faint">Your name, the app version and the data version are attached.</p>
          </dl>
          {error && (
            <p role="alert" className="text-[12px] text-fg">
              {error}
            </p>
          )}
        </div>
      )}
    </Sheet>
  );
}
