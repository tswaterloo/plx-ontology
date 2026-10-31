"use client";

import { AlertTriangle, ArrowLeft, Loader2 } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { fieldEntries } from "@/components/source-chip";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/notice";
import { SkeletonLines } from "@/components/ui/skeleton";
import { getSource } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDateTime, SYSTEM_LABEL } from "@/lib/format";
import type { SourceView } from "@/lib/types";

function SourceReader() {
  const handle = useSearchParams().get("handle") ?? "";
  const [view, setView] = useState<SourceView | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    setView(null);
    getSource(handle, 0, c.signal)
      .then((v) => {
        setView(v);
        setText(v.text ?? "");
      })
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, [handle]);
  useEffect(() => load(), [load]);

  async function loadMore() {
    if (!view?.next_offset) return;
    setMore(true);
    try {
      const next = await getSource(handle, view.next_offset);
      setText((t) => t + (next.text ?? ""));
      setView(next);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setMore(false);
    }
  }

  const fields = view ? fieldEntries(view.fields) : [];
  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
      <button
        type="button"
        onClick={() => (window.history.length > 1 ? window.history.back() : window.location.assign("/"))}
        className="mb-4 inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-sm text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
      >
        <ArrowLeft className="size-4" aria-hidden /> Back
      </button>
      {error && !view ? (
        <ErrorNotice message={error} onRetry={load} />
      ) : !view ? (
        <SkeletonLines lines={6} label="Opening the source" />
      ) : (
        <div className="space-y-4 animate-fade-in">
          <header className="space-y-1">
            <p className="text-xs font-medium uppercase tracking-wide text-muted">{SYSTEM_LABEL[view.system] ?? view.system}</p>
            <h1 className="text-2xl font-semibold tracking-tight" data-testid="source-title">
              {view.title ?? view.label}
            </h1>
            {view.as_of && <p className="text-xs text-muted">As of {formatDateTime(view.as_of)}</p>}
            {view.access_label && (
              <Badge tone="info" data-testid="access-label">{view.access_label}</Badge>
            )}
          </header>
          {view.changed_since && (
            <p className="flex items-center gap-2 rounded-xl border border-warn/40 bg-warn-soft p-3 text-sm">
              <AlertTriangle className="size-4 shrink-0 text-warn" aria-hidden />
              This source has changed since it was cited. Check the answer against what it says now.
            </p>
          )}
          <Card>
            <CardBody className="pt-5">
              {view.kind === "record" || (!text && fields.length > 0) ? (
                <dl className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2" data-testid="source-fields">
                  {fields.map(([k, v]) => (
                    <div key={k}>
                      <dt className="text-xs text-muted">{k}</dt>
                      <dd className="text-sm font-medium tabular-nums">{v}</dd>
                    </div>
                  ))}
                </dl>
              ) : text ? (
                <div className="whitespace-pre-wrap text-[15px] leading-relaxed" data-testid="source-text">
                  {text}
                </div>
              ) : (
                <p className="text-sm text-muted">This source has no readable text.</p>
              )}
              {view.next_offset ? (
                <button
                  type="button"
                  onClick={loadMore}
                  disabled={more}
                  className="mt-4 inline-flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-surface-2 cursor-pointer"
                >
                  {more && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
                  Show more
                </button>
              ) : null}
              {error && view && <ErrorNotice className="mt-3" message={error} />}
            </CardBody>
          </Card>
          <p className="break-all font-mono text-[11px] text-muted">{view.handle}</p>
        </div>
      )}
    </div>
  );
}

export default function SourcePage() {
  return (
    <Suspense fallback={null}>
      <SourceReader />
    </Suspense>
  );
}
