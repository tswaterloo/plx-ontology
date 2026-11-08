"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { LoadProblem } from "@/components/ui/notice";
import { Toolbar } from "@/components/ui/toolbar";
import { compileRule, createRule, deleteRule, getWatchlist, markAlertsRead, setFollowing } from "@/lib/api";
import { ruleParams } from "@/lib/rule-params";
import { ApiError, plainError } from "@/lib/errors";
import { formatDateTime, objectHref, typeLabel } from "@/lib/format";
import type { WatchAlert, Watchlist, WatchRuleDraft } from "@/lib/types";

const EXAMPLES = ["Tell me when any order over $50k slips", "Alert me if a supplier is late twice in a month", "Tell me when a duplicate supplier invoice over $10k is found"];

function said(err: unknown): string {
  return err instanceof ApiError && err.detail && err.status < 500 ? err.detail : plainError(err);
}

function AlertRow({ a }: { a: WatchAlert }) {
  const href = a.record.type ? objectHref(a.record.type, a.record.object_id) : null;
  return (
    <li className="flex items-start gap-3 py-2.5">
      <span aria-hidden className={a.read ? "mt-1.5 size-2 shrink-0 rounded-full" : "mt-1.5 size-2 shrink-0 rounded-full bg-[var(--accent,#2563eb)]"} />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] leading-5 text-fg">{href ? <Link href={href} className="hover:underline">{a.sentence}</Link> : a.sentence}</p>
        <p className="text-[12px] text-muted">
          {formatDateTime(a.at)} · {a.origin === "rule" ? `Rule: ${a.rule_text ?? ""}` : `Following ${a.about.label ?? typeLabel(a.about.type ?? "")}`}
        </p>
      </div>
    </li>
  );
}

/** Watching: the records you follow, your alert rules in plain words and your alerts. */
export default function RulesPage() {
  const [data, setData] = useState<Watchlist | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [text, setText] = useState("");
  const [draft, setDraft] = useState<WatchRuleDraft | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setProblem(null);
    getWatchlist().then(setData, setProblem);
  }, []);
  useEffect(load, [load]);

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
        title={
          <>
            Watching
            {data && data.unread > 0 && <span className="ml-2 text-[13px] font-normal tabular-nums text-faint">{data.unread} new</span>}
          </>
        }
        actions={
          unread.length > 0 ? (
            <button type="button" className="inline-flex h-8 items-center rounded-lg px-2.5 text-[13px] font-medium hover:bg-hover cursor-pointer" onClick={() => run(async () => { await markAlertsRead(unread); load(); })}>
              Mark all read
            </button>
          ) : null
        }
      />
      <div className="mx-auto w-full max-w-3xl space-y-10 overflow-y-auto px-6 py-8">
        {problem ? <LoadProblem message={plainError(problem)} onRetry={load} /> : null}
        {data && !data.configured && <p className="text-[13px] text-muted">Watchlists are not switched on on this server.</p>}

        <section aria-labelledby="new-rule" className="space-y-3">
          <h2 id="new-rule" className="text-[15px] font-semibold">New alert rule</h2>
          <p className="text-[13px] text-muted">Write it in plain words. You see what it will do before it is kept; it only reads what you may read.</p>
          <div className="flex gap-2">
            <input
              aria-label="Alert rule in plain words"
              value={text}
              maxLength={300}
              onChange={(e) => { setText(e.target.value); setDraft(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) preview(); }}
              placeholder={EXAMPLES[0]}
              className="h-9 min-w-0 flex-1 rounded-lg border border-[var(--border,#ddd)] bg-transparent px-3 text-[13px]"
            />
            <button type="button" disabled={busy || !text.trim()} onClick={preview} className="h-9 rounded-lg px-3 text-[13px] font-medium hover:bg-hover cursor-pointer disabled:opacity-50">
              Check
            </button>
          </div>
          <p className="text-[12px] text-faint">
            For example:{" "}
            {EXAMPLES.map((ex, i) => (
              <button key={ex} type="button" className="underline-offset-2 hover:underline cursor-pointer" onClick={() => { setText(ex); setDraft(null); }}>
                {i > 0 ? " · " : ""}“{ex}”
              </button>
            ))}
          </p>
          {refused && <p role="alert" className="text-[13px] text-[var(--danger,#b91c1c)]">{refused}</p>}
          {draft && (
            <div className="space-y-2 rounded-lg border border-[var(--border,#ddd)] p-3">
              <p className="text-[13px] font-medium">{draft.description}</p>
              <dl aria-label="What this rule checks" className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[12px]">
                {ruleParams(draft.spec).map((p) => (
                  <div key={p.name} className="contents"><dt className="text-muted">{p.name}</dt><dd>{p.value}</dd></div>
                ))}
              </dl>
              {draft.markings.length > 0 && <p className="text-[12px] text-muted">Reads {draft.markings.join(", ")} figures: only people cleared for them can set it or see its alerts.</p>}
              <div className="flex gap-2">
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

        <section aria-labelledby="alerts" className="space-y-1">
          <h2 id="alerts" className="text-[15px] font-semibold">Your alerts</h2>
          {data && data.alerts.length === 0 && <p className="text-[13px] text-muted">Nothing yet. Alerts arrive within minutes of a change.</p>}
          <ul className="divide-y divide-[var(--border,#eee)]">{(data?.alerts ?? []).map((a) => <AlertRow key={a.alert_id} a={a} />)}</ul>
        </section>

        <section aria-labelledby="rules" className="space-y-1">
          <h2 id="rules" className="text-[15px] font-semibold">Your rules</h2>
          {data && data.rules.length === 0 && <p className="text-[13px] text-muted">No rules yet.</p>}
          <ul className="divide-y divide-[var(--border,#eee)]">
            {(data?.rules ?? []).map((r) => (
              <li key={r.rule_id} className="flex items-start gap-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] text-fg">{r.text}</p>
                  <p className="text-[12px] text-muted">{r.description}{r.state !== "active" ? ` (${r.state}${r.note ? `: ${r.note}` : ""})` : r.watching_since ? "" : " (starts with the next update)"}</p>
                </div>
                <button type="button" className="h-7 rounded-md px-2 text-[12px] text-muted hover:bg-hover cursor-pointer" onClick={() => run(async () => { await deleteRule(r.rule_id); load(); })}>
                  Delete
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="follows" className="space-y-1">
          <h2 id="follows" className="text-[15px] font-semibold">Following</h2>
          {data && data.follows.length === 0 && <p className="text-[13px] text-muted">Use Follow on any record page.</p>}
          <ul className="divide-y divide-[var(--border,#eee)]">
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
      </div>
    </div>
  );
}
