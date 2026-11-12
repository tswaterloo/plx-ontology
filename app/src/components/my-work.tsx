"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { Button } from "@/components/ui/button";
import { decideAutopilotItem, getAutopilot, prepareMyWorkWrite } from "@/lib/api";
import { dueWords, itemLive, systemLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime, objectHref, plural, typeLabel } from "@/lib/format";
import type { ActionResult, AutopilotArea, MyWork, MyWorkItem } from "@/lib/types";

// Assist: what is open for you and what the autopilot would do about each, worst first.
// Accept records your yes on a dry-run workflow in one click; on a workflow set to Auto it
// opens the review queue, where the item is performed only after its own confirm. A
// message goes out only through its preview and your confirm; an entry is a dry-run write
// intent until writes are granted.

type Link_ = { label?: string; href?: string };
type Group = { key: string; label: string; items: MyWorkItem[] };

const dayOf = (iso: string) => new Date(iso).toLocaleDateString("en-CA", { timeZone: "America/Toronto" });

/** Past due, due today, later: by the due time in Toronto. */
export function groupsOf(items: MyWorkItem[], now: Date = new Date()): Group[] {
  const today = dayOf(now.toISOString());
  const out: Group[] = [
    { key: "past", label: "Past due", items: [] },
    { key: "today", label: "Due today", items: [] },
    { key: "later", label: "Later", items: [] },
  ];
  for (const i of [...items].sort((a, b) => a.due_at.localeCompare(b.due_at))) {
    const t = new Date(i.due_at).getTime();
    out[t < now.getTime() ? 0 : dayOf(i.due_at) === today ? 1 : 2].items.push(i);
  }
  return out.filter((g) => g.items.length);
}

function happenedLine(h: Record<string, unknown>): string {
  return [h.sender, h.subject].filter((x) => typeof x === "string" && x).join(" · ");
}

function Item({ item, area, state, onDone, now }: { item: MyWorkItem; area: AutopilotArea | null; state: ActionResult | null; onDone: (r: ActionResult) => void; now: Date }) {
  const [entry, setEntry] = useState<string | null>(null);
  const [decided, setDecided] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showDraft, setShowDraft] = useState(false);
  const [more, setMore] = useState(false);
  const prepared = (item.prepared ?? {}) as Record<string, unknown>;
  const draft = prepared.draft as { body?: string; subject?: string; to?: { address?: string }[] } | undefined;
  const links = (prepared.links as Link_[] | undefined) ?? [];
  const gaps = (prepared.gaps as string[] | undefined) ?? [];
  const happened = (item.happened ?? {}) as Record<string, unknown>;
  const objectId = typeof happened.object_id === "string" ? happened.object_id : null;
  const objectType = objectId ? objectId.split(":")[0] : null;
  const due = dueWords(item.due_at, now);
  const overdue = due.endsWith("overdue");
  // the studio's own item, when the reader may decide it there (null: no one-click accept)
  const apItem = area?.items?.find((x) => x.item_id === item.item_id && x.mine) ?? null;
  const live = apItem ? itemLive(area!, apItem) : false;
  const accept = async () => {
    setBusy(true);
    try {
      // one click is never a live write: the server refuses a live item sent this way
      const out = await decideAutopilotItem(item.item_id, { decision: "accept", bulk: true });
      setDecided(out.decision?.outcome === "dry_run" ? "Accepted. Dry run: your yes is recorded, nothing was sent or written." : "Accepted.");
    } catch (e) {
      setDecided(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className={cn("grid gap-x-6 gap-y-1.5 border-b border-border py-3 pl-3 text-[13px] sm:grid-cols-[minmax(0,1fr)_auto]", overdue ? "shadow-[inset_3px_0_0_var(--late)]" : "")} data-testid="my-work-item">
      <div className="min-w-0 space-y-1">
        <p className="flex items-baseline gap-2">
          <span className="min-w-0 flex-1 font-medium">{item.title}</span>
          <span className={cn("num shrink-0 text-[12px]", overdue ? "text-late" : "text-muted")} title={formatDateTime(item.due_at)}>
            {due}
          </span>
        </p>
        <p className="break-words text-[12px] text-muted">
          {[item.label !== item.title ? item.label : null, happenedLine(happened) || null].filter(Boolean).join(" · ")}
          {objectId && objectType && (
            <>
              {" · "}
              <Link className="text-fg hover:underline" href={objectHref(objectType, objectId)}>
                {typeLabel(objectType)}
              </Link>
            </>
          )}
          {item.mine ? "" : " · a report's item (view only)"}
        </p>
        {typeof prepared.summary === "string" && (
          <p className="max-w-[72ch]">
            {prepared.summary}
          </p>
        )}
        {links.length > 0 && (
          <p className="flex flex-wrap gap-x-3 text-[12px]">
            {links.map((l) => l.href && (
              <Link key={l.href} className="text-accent hover:underline" href={l.href}>
                {l.label ?? "Open"}
              </Link>
            ))}
          </p>
        )}
        {gaps.length > 0 && <p className="text-[12px] text-muted">Missing: {gaps.join(", ").replaceAll("_", " ")}</p>}
        {draft?.body && showDraft && (
          <div className="mt-1 max-w-[72ch] rounded-[4px] border border-border p-3">
            <p className="mb-1 text-[12px] text-muted">
              Draft{draft.to?.[0]?.address ? ` to ${draft.to[0].address}` : ""}
              {draft.subject ? ` · ${draft.subject}` : ""} · not sent
            </p>
            <p className="whitespace-pre-wrap leading-5">{draft.body}</p>
          </div>
        )}
        <p className="text-[11px] text-faint">
          {plural((item.citations ?? []).length, "source")} cited
          {draft?.body && (
            <>
              {" · "}
              <button type="button" className="text-muted hover:text-fg cursor-pointer" aria-expanded={showDraft} onClick={() => setShowDraft((v) => !v)}>
                {showDraft ? "Hide the draft" : "Show the draft"}
              </button>
            </>
          )}
        </p>
        {decided && (
          <p className="text-[12px] text-muted" role="status">
            {decided}
          </p>
        )}
        {entry && <p className="text-[12px] text-muted">{entry}</p>}
      </div>
      {item.mine && (
        <div className="flex flex-wrap items-start gap-1.5 sm:max-w-[320px] sm:justify-end">
          {apItem && !decided && (apItem.state === "waiting" || apItem.state === "edited") &&
            (live ? (
              <Button size="sm" variant="secondary" asChild>
                <Link href="/autopilot">Review and perform</Link>
              </Button>
            ) : (
              <Button size="sm" disabled={busy} onClick={() => void accept()} data-testid="assist-accept">
                Accept
              </Button>
            ))}
          {apItem && !more && (item.card_id || item.write_ready) && (
            <Button size="sm" variant="ghost" aria-expanded={false} onClick={() => setMore(true)}>
              More
            </Button>
          )}
          {item.card_id && (!apItem || more) && <CardActions card={{ id: item.card_id, subject: { label: item.title }, actions: item.offers }} state={state} onDone={onDone} />}
          {item.write_ready && (!apItem || more) && (
            <Button
              size="sm"
              variant="ghost"
              data-testid="my-work-entry"
              onClick={() => prepareMyWorkWrite(item.item_id).then(() => setEntry("Entry prepared: preview and approve it in the write panel (dry run)."), (e) => setEntry(plainError(e)))}
            >
              Prepare the entry
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

export function MyWorkList({ work }: { work: MyWork }) {
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const [area, setArea] = useState<AutopilotArea | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    // the one-click accept needs the studio's view of the item (live or dry run); none
    // when it cannot be read
    getAutopilot(ctl.signal).then(setArea, () => setArea(null));
    return () => ctl.abort();
  }, []);
  const now = useMemo(() => new Date(), []);
  const items = work.items ?? [];
  const groups = groupsOf(items, now);
  const byAutopilot = Object.entries(work.digest.by_autopilot ?? {}).sort((a, b) => b[1] - a[1]);
  const past = groups.find((g) => g.key === "past")?.items.length ?? 0;
  return (
    <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_300px]" data-testid="my-work">
      <div className="min-w-0 space-y-5">
        {work.status === "off" && <p className="text-[13px] font-medium">The autopilot is switched off: nothing new is prepared and nothing can be approved.</p>}
        <header className="flex flex-wrap items-end gap-x-10 gap-y-3 border-b border-border pb-4">
          <div className="mr-auto">
            <h2 className="text-[15px] font-semibold">What&apos;s open, and what is prepared</h2>
            <p className="text-[12px] text-muted">Nothing is sent or written until you confirm it.</p>
          </div>
          {(
            [
              ["Open", work.digest.open, ""],
              ["Due today", work.digest.due_today, ""],
              ["Past due", past, past ? "text-late" : ""],
            ] as const
          ).map(([k, v, tone]) => (
            <div key={k}>
              <p className="text-[12px] text-muted">{k}</p>
              <p className={cn("num text-[19px] leading-6", tone)}>{v}</p>
            </div>
          ))}
        </header>
        {items.length === 0 ? (
          <p className="text-[13px] text-muted">Nothing is waiting on you.</p>
        ) : (
          groups.map((g) => (
            <section key={g.key} aria-label={g.label}>
              <h3 className={cn("mb-1 text-[12px] font-semibold", g.key === "past" && "text-late")}>
                {g.label} <span className="font-normal text-muted">{g.items.length}</span>
              </h3>
              <ul className="border-t border-border">
                {g.items.map((item) => (
                  <Item key={item.item_id} item={item} area={area} now={now} state={states[item.card_id ?? ""] ?? null} onDone={(r) => setStates((s) => ({ ...s, [item.card_id as string]: r }))} />
                ))}
              </ul>
            </section>
          ))
        )}
      </div>
      <aside className="space-y-8 lg:border-l lg:border-border lg:pl-6">
        <section className="space-y-2">
          <h2 className="text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">Ask for something else</h2>
          <p className="text-[13px] leading-5 text-muted">
            Type it in the Ask box on any page, for example &ldquo;reply to this&rdquo; or &ldquo;chase this supplier every Monday&rdquo;. The page you are on goes with it, and the drafts come back here and to your approvals.
          </p>
          <a className="text-[13px] text-accent hover:underline" href="/ask">
            Open the Ask box
          </a>
        </section>
        {byAutopilot.length > 0 && (
          <section>
            <h2 className="mb-1 text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">Prepared by</h2>
            <ul className="divide-y divide-border text-[13px]">
              {byAutopilot.map(([k, n]) => (
                <li key={k} className="flex justify-between py-1.5">
                  <span>{systemLabel(k)}</span>
                  <span className="num text-muted">{n}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
        <p className="text-[12px] text-muted">
          Every workflow, its mode and its record are in{" "}
          <Link href="/autopilot" className="text-fg underline decoration-border-strong underline-offset-2">
            Autopilot
          </Link>
          .
        </p>
      </aside>
    </div>
  );
}
