"use client";

import { Pending } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { AlertKindsSection } from "@/components/watching/alert-kinds";
import { AlertFeed, FiredKind, firedKinds } from "@/components/watching/fired";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LoadProblem } from "@/components/ui/notice";
import { Toolbar } from "@/components/ui/toolbar";
import { compileRule, createRule, deleteRule, getAlertKinds, getBriefing, getWatchlist, markAlertsRead, setFollowing } from "@/lib/api";
import { ruleParams } from "@/lib/rule-params";
import { cn } from "@/lib/cn";
import { ApiError, plainError } from "@/lib/errors";
import { objectHref, typeLabel } from "@/lib/format";
import type { AlertKinds, Briefing, Watchlist, WatchRuleDraft } from "@/lib/types";

const EXAMPLES = ["Tell me when any order over $50k slips", "Alert me if a supplier is late twice in a month", "Tell me when a duplicate supplier invoice over $10k is found"];

function said(err: unknown): string {
  return err instanceof ApiError && err.detail && err.status < 500 ? err.detail : plainError(err);
}

/** Watching: what fired first (per alert kind, its count, what is new and its records;
 * then your rules' and follows' alerts by day), with your rules, follows and the alert
 * kinds' standing in the rail. */
export default function WatchingPage() {
  const [data, setData] = useState<Watchlist | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [text, setText] = useState("");
  const [draft, setDraft] = useState<WatchRuleDraft | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(() => {
    setProblem(null);
    getWatchlist().then(setData, setProblem);
  }, []);
  useEffect(load, [load]);
  const [reg, setReg] = useState<AlertKinds | null>(null);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [kindsError, setKindsError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retryKinds = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setKindsError(null);
    getAlertKinds(ctl.signal).then(setReg, (e) => !ctl.signal.aborted && setKindsError(e));
    getBriefing(ctl.signal).then(setBriefing, () => undefined);
    return () => ctl.abort();
  }, [round]);
  const fired = useMemo(() => firedKinds(reg, briefing), [reg, briefing]);
  const ruleInput = useRef<HTMLInputElement>(null);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setRefused(null);
    try {
      await fn();
    } catch (err) {
      setRefused(said(err));
    } finally {
      setBusy(false);
    }
  };
  const preview = () => run(async () => setDraft(await compileRule(text.trim())));
  const confirm = () =>
    run(async () => {
      if (!draft) return;
      await createRule(draft);
      setDraft(null);
      setText("");
      load();
    });
  const unread = (data?.alerts ?? []).filter((a) => !a.read).map((a) => a.alert_id);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar
        heading
        title={
          <>
            Watching
            {data && data.unread > 0 && <span className="ml-2 text-[13px] font-normal num text-faint"><span className="sr-only">, </span>{data.unread} new</span>}
          </>
        }
        actions={
          <>
            {unread.length > 0 && (
              <button type="button" className="inline-flex h-8 items-center rounded-lg px-2.5 text-[13px] font-medium hover:bg-hover cursor-pointer" onClick={() => run(async () => { await markAlertsRead(unread); load(); })}>
                Mark all read
              </button>
            )}
            {data?.configured !== false && (
              <button type="button" className="ml-1 inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] cursor-pointer" onClick={() => ruleInput.current?.focus()}>
                New rule
              </button>
            )}
          </>
        }
      />
      <div className="w-full overflow-y-auto px-6 py-5">
        {problem ? <LoadProblem message={plainError(problem)} onRetry={load} /> : null}
        {data && !data.configured && <EmptyState title="Watchlists are off on this server." />}
        <div className="flex flex-wrap items-end gap-x-10 gap-y-3 border-b border-border pb-4" data-testid="watching-figures">
          {(
            [
              ["Firing now", briefing?.status === "read" ? briefing.count : null, ""],
              ["New since your visit", briefing?.status === "read" ? briefing.new_since_visit : null, "text-accent"],
              ["Kinds firing", reg && briefing?.status === "read" ? `${fired.length} of ${reg.kinds.length}` : null, ""],
              ["From your rules and follows", data ? data.alerts.length : null, ""],
              ["Following", data ? data.follows.length : null, ""],
            ] as const
          ).map(([k, v, tone]) => (
            <div key={k}>
              <p className="text-[12px] text-muted">{k}</p>
              <p className={cn("num text-[19px] leading-6", typeof v === "number" && v > 0 && tone)}>{v ?? "·"}</p>
            </div>
          ))}
        </div>
        <div className="grid gap-10 pt-5 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0 space-y-8">
            <section aria-labelledby="fired-title">
              <h2 id="fired-title" className="text-[15px] font-semibold">What fired</h2>
              {kindsError ? (
                <LoadProblem message={plainError(kindsError)} onRetry={retryKinds} />
              ) : !reg || !briefing ? (
                <Pending label="Reading what fired" />
              ) : fired.length === 0 ? (
                <p className="mt-1 text-[13px] text-muted">Nothing fired on the alert kinds you can see.</p>
              ) : (
                <div className="divide-y divide-border">{fired.map((f) => <FiredKind key={f.kind} f={f} />)}</div>
              )}
            </section>
            {data?.configured !== false && (
              <section aria-labelledby="rule-alerts">
                <h2 id="rule-alerts" className="mb-1 text-[15px] font-semibold">From your rules and follows</h2>
                {!data ? problem ? <p className="text-[13px] text-muted">Not loaded: see the note above.</p> : <Pending label="Reading your alerts" /> : data.alerts.length === 0 ? <p className="text-[13px] text-muted">None yet. A rule or a follow shows its changes here.</p> : <AlertFeed alerts={data.alerts} />}
              </section>
            )}
          </div>
          <aside className="space-y-8 lg:border-l lg:border-border lg:pl-6">
            {data?.configured !== false && (
            <>
        <section aria-labelledby="new-rule" className="space-y-3">
          <h2 id="new-rule" className="text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">New alert rule</h2>
          <p className="text-[12px] text-muted">In plain words. You see what it will do before it is kept; it reads only what you may read.</p>
          <div className="flex flex-col gap-2">
            <input
              ref={ruleInput}
              aria-label="Alert rule in plain words"
              value={text}
              maxLength={300}
              onChange={(e) => { setText(e.target.value); setDraft(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) preview(); }}
              placeholder={EXAMPLES[0]}
              className="h-9 min-w-0 shrink-0 rounded-lg border border-control bg-transparent px-3 text-[13px]"
            />
            <button type="button" disabled={busy || !text.trim()} onClick={preview} className="h-8 self-start rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] cursor-pointer disabled:opacity-50">
              {busy && !draft ? "Checking" : "Check"}
            </button>
          </div>
          <p className="text-[12px] text-faint">
            For example:{" "}
            {EXAMPLES.map((ex, i) => (
              <span key={ex}>
                {i > 0 ? " · " : ""}
                <button type="button" className="text-left underline-offset-2 hover:underline cursor-pointer" onClick={() => { setText(ex); setDraft(null); }}>
                  “{ex}”
                </button>
              </span>
            ))}
          </p>
          {refused && <p role="alert" className="text-[13px] text-late">{refused}</p>}
          {draft && (
            <div className="space-y-2 rounded-lg border border-border p-3">
              <p className="text-[13px] font-medium">{draft.description}</p>
              <dl aria-label="Rule conditions" className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[12px]">
                {ruleParams(draft.spec).map((p) => (
                  <div key={p.name} className="contents"><dt className="text-muted">{p.name}</dt><dd>{p.value}</dd></div>
                ))}
              </dl>
              {draft.markings.length > 0 && <p className="text-[12px] text-muted">Reads {draft.markings.join(", ")} figures: only people cleared for them can set it or see its alerts.</p>}
              <div className="flex flex-col gap-2">
                <button type="button" disabled={busy} onClick={confirm} className="h-8 rounded-lg bg-[color-mix(in_srgb,var(--fg)_8%,transparent)] px-3 text-[13px] font-medium cursor-pointer">
                  Keep this rule
                </button>
                <button type="button" onClick={() => setDraft(null)} className="h-8 rounded-lg px-3 text-[13px] text-muted hover:bg-hover cursor-pointer">
                  Cancel
                </button>
              </div>
            </div>
          )}
        </section>

        {(data || !problem) && (
        <>
        <section aria-labelledby="rules" className="space-y-1">
          <h2 id="rules" className="text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">Your rules</h2>
          {data && data.rules.length === 0 && <EmptyState title="No rules yet." />}
          <ul className="divide-y divide-border">
            {(data?.rules ?? []).map((r) => (
              <li key={r.rule_id} className="flex items-start gap-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] text-fg">{r.text}</p>
                  <p className="text-[12px] text-muted">{r.description}{r.state !== "active" ? ` (${r.state}${r.note ? `: ${r.note}` : ""})` : r.watching_since ? "" : " (starts with the next update)"}</p>
                </div>
                {deleting === r.rule_id ? (
                  <span className="flex shrink-0 items-center gap-1">
                    <button type="button" disabled={busy} className="h-7 rounded-md px-2 text-[12px] font-medium text-fg hover:bg-hover cursor-pointer" onClick={() => run(async () => { await deleteRule(r.rule_id); setDeleting(null); load(); })}>
                      Delete this rule
                    </button>
                    <button type="button" className="h-7 rounded-md px-2 text-[12px] text-muted hover:bg-hover cursor-pointer" onClick={() => setDeleting(null)}>
                      Keep
                    </button>
                  </span>
                ) : (
                  <button type="button" className="h-7 rounded-md px-2 text-[12px] text-muted hover:bg-hover cursor-pointer" onClick={() => setDeleting(r.rule_id)}>
                    Delete
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="follows" className="space-y-1">
          <h2 id="follows" className="text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">Following</h2>
          {data && data.follows.length === 0 && <p className="text-[13px] text-muted">Use Follow on any record page.</p>}
          <ul className="divide-y divide-border">
            {(data?.follows ?? []).map((f) => (
              <li key={f.object_id} className="flex items-center gap-3 py-2.5">
                <div className="min-w-0 flex-1 text-[13px]">
                  {f.readable && f.object_type ? <Link href={objectHref(f.object_type, f.object_id)} className="hover:underline">{f.label ?? typeLabel(f.object_type)}</Link> : <span className="text-muted">A record you can no longer open</span>}
                </div>
                <button type="button" className="h-7 rounded-md px-2 text-[12px] text-muted hover:bg-hover cursor-pointer" onClick={() => run(async () => { await setFollowing(f.object_id, false); load(); })}>
                  Unfollow
                </button>
              </li>
            ))}
          </ul>
        </section>
        </>
        )}
            </>
            )}
            {/* a failed read is said once, under What fired */}
            {!kindsError && <AlertKindsSection compact data={{ reg, briefing, error: kindsError, retry: retryKinds }} />}
          </aside>
        </div>
      </div>
    </div>
  );
}
